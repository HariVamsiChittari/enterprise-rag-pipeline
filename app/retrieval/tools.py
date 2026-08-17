"""Agent Framework function tools for ACL-filtered knowledge base retrieval."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Callable, Awaitable

from retrieval.cosmos import RetrievalMode, RetrievedChunk, SecureCosmosRetriever
from retrieval.cosmos_registry import CosmosRegistry
from retrieval.service import _sanitize_chunk


def make_search_tool(
    registry: CosmosRegistry,
    embed_fn: Callable[[str], Awaitable[list[float]]],
    acl_ids: list[str],
    retrieved_chunks: list[RetrievedChunk],
    *,
    acl_enabled: bool = True,
    usage: list[dict[str, Any]] | None = None,
):
    """Create a search tool closure bound to the caller's ACL.

    Fans out retrieval across all registered Cosmos instances and deduplicates.
    Retrieved chunks are appended to `retrieved_chunks` for citation extraction.
    """

    async def search_knowledge_base(
        query: Annotated[str, "Search query describing what information to find"],
        mode: Annotated[str, "Retrieval mode: hybrid, vector, or full_text"] = "hybrid",
    ) -> str:
        """Search the enterprise knowledge base for documents the caller is authorized to access.

        Returns the top relevant chunks with source attribution. Use this tool
        when the user asks a factual question that requires grounding from
        indexed documents.
        """
        retrieval_mode = RetrievalMode(mode) if mode in RetrievalMode.__members__.values() else RetrievalMode.HYBRID
        embedding: list[float] = []
        if retrieval_mode in {RetrievalMode.HYBRID, RetrievalMode.VECTOR}:
            embedding = await embed_fn(query)

        # Timing excludes embed (tracked separately in caller)
        tool_start = time.perf_counter()

        async def _retrieve_from(retriever: SecureCosmosRetriever) -> list[RetrievedChunk]:
            return await asyncio.to_thread(
                retriever.retrieve,
                query_text=query,
                embedding=embedding,
                principal_ids=acl_ids if acl_enabled else [],
                mode=retrieval_mode,
                top_k=5,
            )

        results = await asyncio.gather(
            *(_retrieve_from(r) for _sid, r in registry.items()),
            return_exceptions=True,
        )

        seen: set[str] = set()
        chunks: list[RetrievedChunk] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            for chunk in result:
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    chunks.append(chunk)
                    if len(chunks) == 5:
                        break
            if len(chunks) == 5:
                break

        retrieved_chunks.extend(chunks)

        if usage is not None:
            usage.append({
                "operation": "tool_invocation",
                "tool": "search_knowledge_base",
                "model": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "query": query[:200],
                "chunks_returned": len(chunks),
                "latency_ms": int((time.perf_counter() - tool_start) * 1000),
            })

        if not chunks:
            return "No authorized documents found matching this query."

        return "\n\n".join(
            f"[Source {i}] {chunk.source_name}, page {chunk.page_number} ({chunk.source_url})\n{_sanitize_chunk(chunk.content)}"
            for i, chunk in enumerate(chunks, start=1)
        )

    return search_knowledge_base
