from __future__ import annotations

from unittest.mock import Mock

import pytest

from retrieval.cosmos import RetrievalMode, SecureCosmosRetriever


def candidate() -> dict[str, object]:
    return {
        "id": "chunk",
        "documentId": "document",
        "sourceRunId": "sharepoint-drive:run1",
        "content": "authorized content",
        "sourceName": "document.pdf",
        "sourceUrl": "https://example.sharepoint.com/sites/docs/document.pdf",
        "pageStart": 2,
    }


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_every_ranked_query_filters_acl_before_ranking(mode: RetrievalMode) -> None:
    chunks = Mock()
    chunks.query_items.return_value = [candidate()]
    manifests = Mock()
    manifests.read_item.return_value = {
        "status": "ready",
    }
    retriever = SecureCosmosRetriever(chunks, manifests)

    results = retriever.retrieve(
        "policy",
        [0.1, 0.2],
        ["group-2", "group-1"],
        mode=mode,
    )

    assert [result.content for result in results] == ["authorized content"]
    query = chunks.query_items.call_args.kwargs["query"]
    assert "EXISTS" in query
    assert "ARRAY_CONTAINS(@principalIds, gid)" in query
    assert query.index("WHERE") < query.index("ORDER BY")
    parameters = {
        parameter["name"]: parameter["value"]
        for parameter in chunks.query_items.call_args.kwargs["parameters"]
    }
    assert parameters["@principalIds"] == ["group-1", "group-2"]
    manifests.read_item.assert_called_once_with(
        item="document", partition_key="sharepoint-drive:run1",
    )


def test_non_ready_document_is_not_returned() -> None:
    chunks = Mock()
    chunks.query_items.return_value = [candidate()]
    manifests = Mock()
    manifests.read_item.return_value = {
        "status": "failed",
    }

    assert SecureCosmosRetriever(chunks, manifests).retrieve(
        "policy", [0.1], ["group"]
    ) == []


def test_citation_uses_active_manifest_source_name_with_legacy_fallback() -> None:
    chunks = Mock()
    chunks.query_items.return_value = [candidate()]
    manifests = Mock()
    manifests.read_item.side_effect = [
        {
            "status": "ready",
            "sourceName": "renamed.pdf",
        },
        {"status": "ready"},
    ]
    retriever = SecureCosmosRetriever(chunks, manifests)

    renamed = retriever.retrieve("policy", [0.1], ["group"])
    legacy = retriever.retrieve("policy", [0.1], ["group"])

    assert renamed[0].source_name == "renamed.pdf"
    assert legacy[0].source_name == "document.pdf"


def test_empty_principals_fail_before_query() -> None:
    chunks = Mock()
    with pytest.raises(ValueError, match="principal_ids_required"):
        SecureCosmosRetriever(chunks, Mock()).retrieve("policy", [0.1], [])
    chunks.query_items.assert_not_called()


def test_acl_disabled_omits_filter_and_accepts_empty_principals() -> None:
    chunks = Mock()
    chunks.query_items.return_value = [candidate()]
    manifests = Mock()
    manifests.read_item.return_value = {"status": "ready"}
    retriever = SecureCosmosRetriever(chunks, manifests, acl_enabled=False)

    results = retriever.retrieve("policy", [0.1, 0.2], [], mode=RetrievalMode.HYBRID)

    assert len(results) == 1
    query = chunks.query_items.call_args.kwargs["query"]
    assert "allowedGroupIds" not in query
    assert "@principalIds" not in query