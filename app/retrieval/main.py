"""FastAPI application for RAG retrieval service on AKS."""

from __future__ import annotations

import asyncio
import collections
import os
import time
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import httpx
import structlog
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from openai import AzureOpenAI
from pydantic import BaseModel, Field

from retrieval.auth import (
    AuthorizationError,
    GraphGroupResolver,
    Principal,
    principal_from_easy_auth,
)
from retrieval.config import RetrievalConfig, load_retrieval_config
from retrieval.cosmos import RetrievalMode, RetrievedChunk
from retrieval.cosmos_registry import CosmosRegistry, build_cosmos_registry, load_cosmos_instance_configs
from retrieval.service import RagService
from retrieval.telemetry import write_audit_records

try:
    from retrieval.agent import create_rag_agent
    from retrieval.tools import make_search_tool
    _AGENT_AVAILABLE = True
except ImportError:
    _AGENT_AVAILABLE = False

logger = structlog.get_logger()


class _TokenRefreshAuth(httpx.Auth):
    """httpx auth handler that refreshes MI tokens before expiry."""

    def __init__(self, credential: ManagedIdentityCredential) -> None:
        self._credential = credential
        self._scope = "https://graph.microsoft.com/.default"

    def auth_flow(self, request: httpx.Request):
        token = self._credential.get_token(self._scope)
        request.headers["Authorization"] = f"Bearer {token.token}"
        yield request


class _AppState:
    config: RetrievalConfig
    registry: CosmosRegistry
    credential: ManagedIdentityCredential
    openai_client: AzureOpenAI
    rag_service: RagService
    group_resolver: GraphGroupResolver
    audit_container: Any
    agent_chat_client: Any


_state = _AppState()


def _configure_tracing(config: RetrievalConfig) -> None:
    """Best-effort GenAI OpenTelemetry tracing (gen_ai.usage.* spans in App Insights).

    No-ops if APPLICATIONINSIGHTS_CONNECTION_STRING isn't configured, so existing
    deployments without it wired keep working unchanged.
    """
    if not config.app_insights_connection_string:
        logger.info("tracing_not_configured", reason="APPLICATIONINSIGHTS_CONNECTION_STRING unset")
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

        configure_azure_monitor(connection_string=config.app_insights_connection_string)
        OpenAIInstrumentor().instrument()
        logger.info("tracing_configured")
    except Exception:
        logger.warning("tracing_configuration_failed", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config = load_retrieval_config()
    _configure_tracing(config)
    credential = ManagedIdentityCredential(client_id=config.managed_identity_client_id)

    instance_configs = load_cosmos_instance_configs(
        default_source_id=os.getenv("INGESTION_SOURCE_ID", "default"),
        default_endpoint=config.cosmos_endpoint,
        default_database=config.cosmos_database,
        default_chunks_container=config.cosmos_chunks_container,
        default_manifests_container=config.cosmos_manifests_container,
    )
    registry = build_cosmos_registry(instance_configs, credential)

    cosmos = CosmosClient(url=config.cosmos_endpoint, credential=credential)
    db = cosmos.get_database_client(config.cosmos_database)
    audit_container = db.get_container_client(config.cosmos_audit_container)

    _state.config = config
    _state.credential = credential
    _state.registry = registry
    _state.audit_container = audit_container

    def _openai_token_provider() -> str:
        return credential.get_token("https://cognitiveservices.azure.com/.default").token

    _state.openai_client = AzureOpenAI(
        azure_endpoint=config.openai_endpoint,
        azure_ad_token_provider=_openai_token_provider,
        api_version=config.openai_api_version,
        max_retries=2,
    )

    if _AGENT_AVAILABLE:
        try:
            from agent_framework.openai import OpenAIChatClient
            from openai import AsyncAzureOpenAI

            async_openai = AsyncAzureOpenAI(
                azure_endpoint=config.openai_endpoint,
                azure_ad_token_provider=_openai_token_provider,
                api_version=config.agent_api_version,
                max_retries=2,
            )
            _state.agent_chat_client = OpenAIChatClient(
                config.chat_deployment, async_client=async_openai,
            )
        except Exception:
            logger.warning("agent_chat_client_init_failed", exc_info=True)
            _state.agent_chat_client = None
    else:
        _state.agent_chat_client = None

    _state.rag_service = RagService(
        _state.openai_client,
        _state.registry,
        config.embedding_deployment,
        config.chat_deployment,
        retrieval_timeout_seconds=config.retrieval_timeout_seconds,
        generation_timeout_seconds=config.generation_timeout_seconds,
        max_evidence=config.max_evidence_chunks,
        max_planned_queries=config.max_planned_queries,
    )
    _state.group_resolver = GraphGroupResolver(
        httpx.Client(auth=_TokenRefreshAuth(credential), timeout=config.graph_group_timeout_seconds)
    )

    logger.info(
        "retrieval_service_started",
        cosmos_endpoint=config.cosmos_endpoint,
        cosmos_instances=len(registry),
    )
    yield
    _state.rag_service.close()
    logger.info("retrieval_service_stopped")


app = FastAPI(title="RAG Retrieval Agent", lifespan=_lifespan)

_RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "30"))
_RATE_LIMIT_WINDOW = 60.0


class _SlidingWindowLimiter:
    """Per-user sliding window rate limiter (in-memory, per-instance)."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        self._requests: dict[str, collections.deque] = {}

    def is_allowed(self, user_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._requests.setdefault(user_id, collections.deque())
            while window and window[0] <= now - self._window:
                window.popleft()
            if len(window) >= self._max:
                return False
            window.append(now)
            # Evict users with no recent requests to bound memory
            if len(self._requests) > 10_000:
                stale = [k for k, v in self._requests.items() if not v]
                for k in stale:
                    del self._requests[k]
            return True


_rate_limiter = _SlidingWindowLimiter(_RATE_LIMIT_RPM, _RATE_LIMIT_WINDOW)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list)
    mode: str = Field(default="hybrid", pattern="^(hybrid|vector|full_text)$")
    top_k: int | None = Field(default=None, ge=1, le=20)


class Citation(BaseModel):
    ref: str
    source_name: str
    url: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    request_id: str


@app.post("/api/query", response_model=QueryResponse)
async def query(request: Request, body: QueryRequest, background_tasks: BackgroundTasks):
    request_id = str(uuid.uuid4())
    start = time.perf_counter()
    log = logger.bind(request_id=request_id)

    principal = _resolve_principal(request)

    if not _rate_limiter.is_allowed(principal.user_id):
        raise HTTPException(status_code=429, detail="rate_limit_exceeded")

    mode = RetrievalMode(body.mode)

    queries, planning_usage = await asyncio.to_thread(
        _state.rag_service.plan_queries, body.question, body.history or None,
    )

    use_agent = len(queries) >= 2 and _state.agent_chat_client is not None and _AGENT_AVAILABLE
    path = "standard"

    if use_agent:
        log.info("query_start", user_id=principal.user_id, mode=body.mode, path="agentic",
                 planned_queries=len(queries))
        result = await _run_agentic_path(
            body.question, principal, mode, planning_usage, log,
        )
        if result is not None:
            path = "agentic"
        else:
            path = "agentic_fallback"
            log.warning("agentic_path_fallback")

    if not use_agent or result is None:
        log.info("query_start", user_id=principal.user_id, mode=body.mode, path=path,
                 planned_queries=len(queries))
        result = await asyncio.to_thread(
            _state.rag_service.answer_with_queries,
            body.question, queries, principal, mode, planning_usage,
            body.top_k,
        )

    background_tasks.add_task(
        write_audit_records,
        _state.audit_container,
        request_id,
        principal.user_id,
        principal.tenant_id,
        body.mode,
        result.get("usage", []),
    )

    elapsed = time.perf_counter() - start
    log.info("query_complete", latency_ms=int(elapsed * 1000),
             chunks=len(result["citations"]), path=path)

    answer_text = result["answer"]
    background_tasks.add_task(
        _write_query_summary,
        _state.audit_container,
        request_id, principal.user_id, principal.tenant_id,
        body.question, answer_text, len(result["citations"]),
        path, body.mode, len(queries), int(elapsed * 1000),
    )

    return QueryResponse(
        answer=result["answer"],
        citations=[
            Citation(
                ref=f"[S{i}]",
                source_name=c["source_name"],
                url=f"{c['source_url']}#page={c['page_number']}" if c.get("source_url") else f"{c['source_name']}#page={c['page_number']}",
            )
            for i, c in enumerate(result["citations"], start=1)
        ] if _state.config.include_citations else [],
        request_id=request_id,
    )


def _write_query_summary(
    container: Any, request_id: str, user_id: str, tenant_id: str,
    question: str, answer: str, citations_count: int,
    path: str, mode: str, planned_queries: int, e2e_latency_ms: int,
) -> None:
    from retrieval.telemetry import write_audit_records
    write_audit_records(container, request_id, user_id, tenant_id, mode, [{
        "operation": "query_request",
        "question": question[:2000],
        "question_truncated": len(question) > 2000,
        "answer_preview": answer[:500],
        "answer_truncated": len(answer) > 500,
        "citations_count": citations_count,
        "path": path,
        "planned_queries": planned_queries,
        "e2e_latency_ms": e2e_latency_ms,
    }])


async def _run_agentic_path(
    question: str,
    principal: Principal,
    mode: RetrievalMode,
    planning_usage: list[dict[str, Any]],
    log: Any,
) -> dict[str, Any] | None:
    """Run the Agent Framework agent. Returns None on timeout/error (caller falls back)."""
    try:
        async def _embed(text: str) -> list[float]:
            resp = await asyncio.to_thread(
                _state.openai_client.embeddings.create,
                model=_state.config.embedding_deployment,
                input=[text],
                dimensions=3072,
                encoding_format="float",
            )
            return list(resp.data[0].embedding)

        retrieved_chunks: list[RetrievedChunk] = []
        search_tool = make_search_tool(
            registry=_state.registry,
            embed_fn=_embed,
            acl_ids=list(principal.acl_ids),
            retrieved_chunks=retrieved_chunks,
        )
        agent = create_rag_agent(
            _state.agent_chat_client, search_tool, model=_state.config.chat_deployment,
        )
        response = await asyncio.wait_for(
            agent.run(question),
            timeout=_state.config.agent_timeout_seconds,
        )
        answer_text = str(response)
        if not answer_text.strip():
            log.warning("agent_empty_response")
            return None

        return {
            "answer": answer_text.strip(),
            "citations": [asdict(c) for c in retrieved_chunks],
            "usage": planning_usage,
        }
    except asyncio.TimeoutError:
        log.warning("agent_timeout", timeout=_state.config.agent_timeout_seconds)
        return None
    except Exception:
        log.warning("agent_error", exc_info=True)
        return None


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    try:
        _source_id, retriever = _state.registry.items()[0]
        items = retriever._chunks.query_items(
            query="SELECT TOP 1 c.id FROM c",
            enable_cross_partition_query=True,
            max_item_count=1,
        )
        await asyncio.to_thread(lambda: list(items))
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="cosmos_unavailable")


def _resolve_principal(request: Request) -> Principal:
    encoded = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not encoded:
        raise HTTPException(status_code=401, detail="missing_auth_header")
    try:
        return principal_from_easy_auth(
            encoded,
            expected_tenant_id=_state.config.tenant_id,
            group_resolver=_state.group_resolver,
        )
    except AuthorizationError as e:
        raise HTTPException(status_code=401, detail=str(e))
