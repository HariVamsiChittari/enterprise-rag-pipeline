"""ACL-filtered Cosmos DB retrieval and publication validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from azure.cosmos.exceptions import CosmosResourceNotFoundError


class RetrievalMode(StrEnum):
    HYBRID = "hybrid"
    VECTOR = "vector"
    FULL_TEXT = "full_text"


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    content: str
    source_name: str
    page_number: int


_PROJECTION = (
    "c.id, c.documentId, c.publicationVersion, c.content, "
    "c.sourceName, c.pageNumber"
)
_ACL_FILTER = (
    "EXISTS(SELECT VALUE principalId FROM principalId IN c.aclPrincipalIds "
    "WHERE ARRAY_CONTAINS(@principalIds, principalId))"
)
_RANKING = {
    RetrievalMode.HYBRID: (
        "RRF(VectorDistance(c.embedding, @embedding), "
        "FullTextScore(c.content, @searchText))"
    ),
    RetrievalMode.VECTOR: "VectorDistance(c.embedding, @embedding)",
    RetrievalMode.FULL_TEXT: "FullTextScore(c.content, @searchText)",
}


class SecureCosmosRetriever:
    def __init__(self, chunks: Any, manifests: Any) -> None:
        self._chunks = chunks
        self._manifests = manifests

    def retrieve(
        self,
        query_text: str,
        embedding: list[float],
        principal_ids: list[str],
        *,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        if not query_text.strip():
            raise ValueError("query_text_required")
        if not principal_ids:
            raise ValueError("principal_ids_required")
        if top_k < 1 or top_k > 50:
            raise ValueError("top_k_out_of_range")
        if mode in {RetrievalMode.HYBRID, RetrievalMode.VECTOR} and not embedding:
            raise ValueError("embedding_required")

        query = (
            f"SELECT TOP @topK {_PROJECTION} FROM c WHERE {_ACL_FILTER} "
            f"ORDER BY RANK {_RANKING[mode]}"
        )
        parameters = [
            {"name": "@topK", "value": top_k},
            {"name": "@principalIds", "value": sorted(set(principal_ids))},
            {"name": "@searchText", "value": query_text},
            {"name": "@embedding", "value": embedding},
        ]
        candidates = self._chunks.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True,
        )
        results: list[RetrievedChunk] = []
        for candidate in candidates:
            manifest = self._active_manifest(candidate)
            if manifest is None:
                continue
            manifest_source_name = manifest.get("sourceName")
            results.append(
                _to_chunk(
                    candidate,
                    manifest_source_name
                    if isinstance(manifest_source_name, str) and manifest_source_name
                    else None,
                )
            )
        return results

    def _active_manifest(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        document_id = candidate.get("documentId")
        publication = candidate.get("publicationVersion")
        if not isinstance(document_id, str) or not isinstance(publication, str):
            return None
        try:
            manifest = self._manifests.read_item(
                item=document_id,
                partition_key=document_id,
            )
        except CosmosResourceNotFoundError:
            return None
        if not (
            manifest.get("state") == "queryable"
            and manifest.get("publicationVersion") == publication
        ):
            return None
        return manifest


def _to_chunk(
    candidate: dict[str, Any],
    manifest_source_name: str | None = None,
) -> RetrievedChunk:
    values = {
        "chunk_id": candidate.get("id"),
        "document_id": candidate.get("documentId"),
        "content": candidate.get("content"),
        "source_name": manifest_source_name or candidate.get("sourceName"),
        "page_number": candidate.get("pageNumber"),
    }
    if (
        not all(isinstance(values[name], str) and values[name] for name in values if name != "page_number")
        or not isinstance(values["page_number"], int)
        or values["page_number"] < 1
    ):
        raise ValueError("invalid_retrieval_record")
    return RetrievedChunk(**values)