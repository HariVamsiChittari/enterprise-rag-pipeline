from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from retrieval.auth import Principal
from retrieval.cosmos import RetrievalMode, RetrievedChunk
from retrieval.cosmos_registry import CosmosRegistry
from retrieval.service import RagService


def _registry(retriever) -> CosmosRegistry:
    return CosmosRegistry({"source": retriever})


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
        [RetrievedChunk("1", "doc", "a", "a.pdf", "https://sp.com/a.pdf", 1)],
        [RetrievedChunk("1", "doc", "a", "a.pdf", "https://sp.com/a.pdf", 1), RetrievedChunk("2", "doc", "b", "b.pdf", "https://sp.com/b.pdf", 2)],
        [],
    ]
    service = RagService(client, _registry(retriever), "embedding", "chat")

    result = service.answer(
        "follow up",
        Principal("user", "tenant", frozenset({"group"})),
        [{"role": "user", "content": "earlier"}],
    )

    assert retriever.retrieve.call_count == 3
    assert sorted(call.args[0] for call in retriever.retrieve.call_args_list) == ["one", "three", "two"]
    assert len(result["citations"]) == 2


def test_service_does_not_call_answer_model_without_evidence() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion('{"queries":["question"]}')
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)]
    )
    retriever = Mock()
    retriever.retrieve.return_value = []

    result = RagService(client, _registry(retriever), "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
    )

    assert result["citations"] == []
    assert client.chat.completions.create.call_count == 1


def test_full_text_mode_does_not_create_embedding() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion('{"queries":["question"]}')
    retriever = Mock()
    retriever.retrieve.return_value = []

    RagService(client, _registry(retriever), "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
        mode=RetrievalMode.FULL_TEXT,
    )

    client.embeddings.create.assert_not_called()
    assert retriever.retrieve.call_args.args[1] == []


def test_usage_is_tracked_for_embedding_and_generation_calls() -> None:
    client = Mock()
    client.chat.completions.create.side_effect = [
        completion('{"queries":["question"]}'),
        completion("Grounded [S1]."),
    ]
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=0),
    )
    retriever = Mock()
    retriever.retrieve.return_value = [RetrievedChunk("1", "doc", "a", "a.pdf", "https://sp.com/a.pdf", 1)]

    result = RagService(client, _registry(retriever), "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
    )

    operations = {record["operation"] for record in result["usage"]}
    assert operations == {"query_planning", "embedding", "answer_generation"}
    embedding_record = next(r for r in result["usage"] if r["operation"] == "embedding")
    assert embedding_record["prompt_tokens"] == 12
    assert embedding_record["model"] == "embedding"
    assert isinstance(embedding_record["latency_ms"], int)


def test_usage_is_returned_even_without_evidence() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion('{"queries":["question"]}')
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=0),
    )
    retriever = Mock()
    retriever.retrieve.return_value = []

    result = RagService(client, _registry(retriever), "embedding", "chat").answer(
        "question",
        Principal("user", "tenant", frozenset({"group"})),
    )

    assert result["citations"] == []
    assert result["usage"][0]["operation"] == "query_planning"
    assert result["usage"][1]["operation"] == "embedding"
    assert client.chat.completions.create.call_count == 1


def test_slow_retrieval_query_is_dropped_after_timeout_budget() -> None:
    import time

    client = Mock()
    client.chat.completions.create.side_effect = [
        completion('{"queries":["fast","slow"]}'),
        completion("Grounded [S1]."),
    ]
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.0] * 3072)]
    )
    retriever = Mock()

    def _retrieve(query, *_args, **_kwargs):
        if query == "slow":
            time.sleep(0.3)
            return [RetrievedChunk("2", "doc", "b", "b.pdf", "https://sp.com/b.pdf", 2)]
        return [RetrievedChunk("1", "doc", "a", "a.pdf", "https://sp.com/a.pdf", 1)]

    retriever.retrieve.side_effect = _retrieve
    service = RagService(
        client, _registry(retriever), "embedding", "chat",
        retrieval_timeout_seconds=0.05,
        generation_timeout_seconds=1.0,
    )

    result = service.answer(
        "follow up",
        Principal("user", "tenant", frozenset({"group"})),
        [{"role": "user", "content": "earlier"}],
    )

    assert [c["chunk_id"] for c in result["citations"]] == ["1"]


def test_plan_queries_returns_single_for_simple_question() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion('{"queries":["What is RAG?"]}')
    retriever = Mock()
    service = RagService(client, _registry(retriever), "embedding", "chat")

    queries, usage = service.plan_queries("What is RAG?")

    assert queries == ["What is RAG?"]
    assert usage[0]["operation"] == "query_planning"


def test_plan_queries_decomposes_complex_question() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion(
        '{"queries":["security policy details","data governance policy details"]}'
    )
    retriever = Mock()
    service = RagService(client, _registry(retriever), "embedding", "chat")

    queries, usage = service.plan_queries(
        "Compare our security policy with our data governance policy"
    )

    assert len(queries) == 2
    assert usage[0]["operation"] == "query_planning"


def test_plan_queries_uses_history_as_context() -> None:
    client = Mock()
    client.chat.completions.create.return_value = completion('{"queries":["standalone question"]}')
    retriever = Mock()
    service = RagService(client, _registry(retriever), "embedding", "chat")

    queries, _ = service.plan_queries(
        "Tell me more about that",
        [{"role": "user", "content": "What is our leave policy?"}],
    )

    assert queries == ["standalone question"]
    call_content = client.chat.completions.create.call_args[1]["messages"][1]["content"]
    assert "history" in call_content