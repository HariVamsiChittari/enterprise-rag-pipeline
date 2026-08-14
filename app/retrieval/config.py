"""Retrieval service configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(f"Required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class RetrievalConfig:
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_chunks_container: str
    cosmos_manifests_container: str
    cosmos_audit_container: str
    openai_endpoint: str
    embedding_deployment: str
    chat_deployment: str
    tenant_id: str
    managed_identity_client_id: str
    retrieval_timeout_seconds: float
    generation_timeout_seconds: float
    agent_timeout_seconds: float
    agent_max_iterations: int
    agent_api_version: str
    max_evidence_chunks: int
    max_planned_queries: int
    graph_group_timeout_seconds: float
    openai_api_version: str
    app_insights_connection_string: str | None
    include_citations: bool


def load_retrieval_config() -> RetrievalConfig:
    return RetrievalConfig(
        cosmos_endpoint=_required("COSMOS_ENDPOINT"),
        cosmos_database=_required("COSMOS_DATABASE"),
        cosmos_chunks_container=os.getenv("COSMOS_CHUNKS_CONTAINER", "search-chunks"),
        cosmos_manifests_container=os.getenv("COSMOS_MANIFESTS_CONTAINER", "source-documents"),
        cosmos_audit_container=os.getenv("COSMOS_AUDIT_CONTAINER", "service-audit"),
        openai_endpoint=_required("AZURE_OPENAI_ENDPOINT"),
        embedding_deployment=os.getenv("EMBEDDING_DEPLOYMENT", "text-embedding-3-large"),
        chat_deployment=_required("CHAT_DEPLOYMENT"),
        tenant_id=_required("TENANT_ID"),
        managed_identity_client_id=_required("MANAGED_IDENTITY_CLIENT_ID"),
        retrieval_timeout_seconds=float(os.getenv("RETRIEVAL_TIMEOUT_SECONDS", "5.0")),
        generation_timeout_seconds=float(os.getenv("GENERATION_TIMEOUT_SECONDS", "3.0")),
        agent_timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "8.0")),
        agent_max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "5")),
        agent_api_version=os.getenv("AGENT_OPENAI_API_VERSION", "2025-04-01-preview"),
        max_evidence_chunks=int(os.getenv("MAX_EVIDENCE_CHUNKS", "5")),
        max_planned_queries=int(os.getenv("MAX_PLANNED_QUERIES", "3")),
        graph_group_timeout_seconds=float(os.getenv("GRAPH_GROUP_TIMEOUT_SECONDS", "10.0")),
        openai_api_version=os.getenv("OPENAI_API_VERSION", "2024-10-21"),
        app_insights_connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip() or None,
        include_citations=os.getenv("INCLUDE_CITATIONS", "true").strip().lower() != "false",
    )
