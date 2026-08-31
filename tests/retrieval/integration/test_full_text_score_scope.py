"""Env-gated live-Cosmos smoke tests for Phase 2a retrieval features.

Skipped by default so `pytest` runs green without a live container. To run:

  $env:RAG_INTEGRATION_COSMOS_ENDPOINT="https://<account>.documents.azure.com:443/"
  $env:RAG_INTEGRATION_COSMOS_DATABASE="rag-db"
  $env:RAG_INTEGRATION_COSMOS_CHUNKS_CONTAINER="search-chunks"
  $env:RAG_INTEGRATION_COSMOS_MANIFESTS_CONTAINER="source-documents"
  python -m pytest tests/retrieval/integration/test_full_text_score_scope.py -q

Uses DefaultAzureCredential (`az login` locally, or Managed Identity in Azure) per
AGENTS.md. Verifies structural correctness only \u2014 no ordering assertions, so tests
pass whether the container is empty or populated.
"""

from __future__ import annotations

import os

import pytest

_REQUIRED_ENV = (
    "RAG_INTEGRATION_COSMOS_ENDPOINT",
    "RAG_INTEGRATION_COSMOS_DATABASE",
    "RAG_INTEGRATION_COSMOS_CHUNKS_CONTAINER",
    "RAG_INTEGRATION_COSMOS_MANIFESTS_CONTAINER",
)

pytestmark = pytest.mark.skipif(
    any(not os.getenv(var) for var in _REQUIRED_ENV),
    reason="Live-Cosmos integration test; set RAG_INTEGRATION_COSMOS_* env vars to run",
)


@pytest.fixture(scope="module")
def containers():
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    client = CosmosClient(os.environ["RAG_INTEGRATION_COSMOS_ENDPOINT"], credential=credential)
    db = client.get_database_client(os.environ["RAG_INTEGRATION_COSMOS_DATABASE"])
    yield (
        db.get_container_client(os.environ["RAG_INTEGRATION_COSMOS_CHUNKS_CONTAINER"]),
        db.get_container_client(os.environ["RAG_INTEGRATION_COSMOS_MANIFESTS_CONTAINER"]),
    )


def _retriever(containers):
    from retrieval.cosmos import SecureCosmosRetriever

    chunks, manifests = containers
    return SecureCosmosRetriever(chunks, manifests, acl_enabled=False)


def _dummy_embedding() -> list[float]:
    return [0.0] * 3072


@pytest.mark.parametrize("scope", ["Local", "Global"])
def test_full_text_score_scope_completes_without_error(containers, scope) -> None:
    """Both Local and Global scopes must produce valid queries against a real container."""
    result = _retriever(containers).retrieve(
        "policy",
        _dummy_embedding(),
        [],
        top_k=5,
        full_text_score_scope=scope,
        raw=True,
    )
    assert isinstance(result, list)
    assert len(result) <= 5


@pytest.mark.parametrize(
    "weights",
    [(10.0, 0.0), (0.0, 10.0), (1.0, 1.0)],
    ids=["vector-only", "bm25-only", "equal"],
)
def test_weighted_rrf_positional_mapping_completes_without_error(containers, weights) -> None:
    """Confirms `RRF(VectorDistance, FullTextScore, @rrfWeights)` positional binding works.

    Verified upstream in
    https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cosmos/azure-cosmos/tests/test_query_hybrid_search.py.
    """
    result = _retriever(containers).retrieve(
        "policy",
        _dummy_embedding(),
        [],
        top_k=5,
        rrf_weights=weights,
        raw=True,
    )
    assert isinstance(result, list)
    assert len(result) <= 5


def test_projection_contains_source_modified_at_field_on_chunk_rows(containers) -> None:
    """Confirms the new `/sourceModifiedAt/?` Bicep index path is populated on chunks."""
    result = _retriever(containers).retrieve(
        "policy",
        _dummy_embedding(),
        [],
        top_k=1,
        raw=True,
    )
    assert isinstance(result, list)
    if result:
        # Field is nullable for legacy chunks; assert only that the key is projected.
        assert "sourceModifiedAt" in result[0]


def test_hybrid_query_with_max_candidate_pool_top_k_stays_under_sdk_cap(containers) -> None:
    """MAX_CANDIDATE_POOL_TOTAL = 50 must stay well under AZURE_COSMOS_HYBRID_SEARCH_MAX_ITEMS = 1000.

    See https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/cosmos/azure-cosmos/README.md.
    """
    from retrieval.cosmos import MAX_CANDIDATE_POOL_TOTAL

    result = _retriever(containers).retrieve(
        "policy",
        _dummy_embedding(),
        [],
        top_k=MAX_CANDIDATE_POOL_TOTAL,
        raw=True,
    )
    assert isinstance(result, list)
    assert len(result) <= MAX_CANDIDATE_POOL_TOTAL
