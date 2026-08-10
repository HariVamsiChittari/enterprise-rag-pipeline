from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from retrieval.auth import Principal
from retrieval.cosmos import RetrievalMode, RetrievedChunk
from retrieval.service import RagService


def completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_service_bounds_planned_queries_and_evidence() -> None:
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion('{"queries":["one","two","three","four"]}'),
        completion("Grounded [S1]."),
    ]
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)]
    )
    retriever = Mock()
    retriever.retrieve.side_effect = [
        [RetrievedChunk("1", "doc", "a", "a.pdf", 1)],
        [RetrievedChunk("1", "doc", "a", "a.pdf", 1), RetrievedChunk("2", "doc", "b", "b.pdf", 2)],
        [],
    ]
    service = RagService(client, retriever, "embedding", "chat")

    result = service.answer(
        "follow up",
        Principal("user", "tenant", frozenset({"group"})),
        [{"role": "user", "content": "earlier"}],
    )

    assert retriever.retrieve.call_count == 3
    assert [call.args[0] for call in retriever.retrieve.call_args_list] == ["one", "two", "three"]
    assert len(result["citations"]) == 2


def test_service_does_not_call_answer_model_without_evidence() -> None:
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)]
    )
    retriever = Mock()
    retriever.retrieve.return_value = []

    result = RagService(client, retriever, "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
    )

    assert result["citations"] == []
    client.chat.completions.create.assert_not_called()


def test_full_text_mode_does_not_create_embedding() -> None:
    client = Mock()
    retriever = Mock()
    retriever.retrieve.return_value = []

    RagService(client, retriever, "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
        mode=RetrievalMode.FULL_TEXT,
    )

    client.embeddings.create.assert_not_called()
    assert retriever.retrieve.call_args.args[1] == []