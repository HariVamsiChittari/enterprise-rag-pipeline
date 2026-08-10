"""Language AI enrichment — summary, key phrases, entities (configurable)."""

from __future__ import annotations

import logging
from typing import Any

from ingestion.models import Entity, EnrichmentStatuses, ModuleStatus

logger = logging.getLogger(__name__)

# Azure AI Language API limits: entities=5, key_phrases=10. Use lowest.
LANGUAGE_BATCH_SIZE = 5


def enrich_chunks(
    client: Any | None,
    chunks_text: list[str],
    *,
    summary_enabled: bool = True,
    key_phrases_enabled: bool = True,
    entities_enabled: bool = True,
    max_key_phrases: int = 25,
    max_entities: int = 50,
) -> list[dict[str, Any]]:
    """Enrich chunk texts with Language AI modules. Returns one dict per chunk."""
    if client is None or not any([summary_enabled, key_phrases_enabled, entities_enabled]):
        return [_skipped_enrichment() for _ in chunks_text]

    results: list[dict[str, Any]] = []
    for offset in range(0, len(chunks_text), LANGUAGE_BATCH_SIZE):
        batch = chunks_text[offset : offset + LANGUAGE_BATCH_SIZE]
        batch_results = _enrich_batch(
            client, batch,
            summary_enabled=summary_enabled,
            key_phrases_enabled=key_phrases_enabled,
            entities_enabled=entities_enabled,
            max_key_phrases=max_key_phrases,
            max_entities=max_entities,
        )
        results.extend(batch_results)
    return results


def _enrich_batch(
    client: Any,
    texts: list[str],
    *,
    summary_enabled: bool,
    key_phrases_enabled: bool,
    entities_enabled: bool,
    max_key_phrases: int,
    max_entities: int,
) -> list[dict[str, Any]]:
    documents = [{"id": str(i), "text": text} for i, text in enumerate(texts)]
    batch_results: list[dict[str, Any]] = []

    summaries: list[str | None] = [None] * len(texts)
    key_phrases_list: list[tuple[str, ...]] = [() for _ in texts]
    entities_list: list[tuple[Entity, ...]] = [() for _ in texts]
    statuses: list[dict[str, ModuleStatus]] = [
        {"summary": ModuleStatus.NOT_REQUESTED, "key_phrases": ModuleStatus.NOT_REQUESTED, "entities": ModuleStatus.NOT_REQUESTED}
        for _ in texts
    ]

    if summary_enabled:
        try:
            results = list(client.begin_abstract_summary([t for t in texts]).result())
            for i, result in enumerate(results):
                if getattr(result, "is_error", False):
                    statuses[i]["summary"] = ModuleStatus.FAILED
                else:
                    summary_texts = [
                        getattr(s, "text", "").strip()
                        for s in getattr(result, "summaries", [])
                        if isinstance(getattr(s, "text", None), str)
                    ]
                    summaries[i] = " ".join(t for t in summary_texts if t) or None
                    statuses[i]["summary"] = ModuleStatus.SUCCEEDED
        except Exception:
            logger.warning("Summary enrichment failed for batch", exc_info=True)
            for i in range(len(texts)):
                statuses[i]["summary"] = ModuleStatus.FAILED

    if key_phrases_enabled:
        try:
            results = client.extract_key_phrases(documents)
            for i, result in enumerate(results):
                if getattr(result, "is_error", False):
                    statuses[i]["key_phrases"] = ModuleStatus.FAILED
                else:
                    phrases = getattr(result, "key_phrases", [])
                    seen: set[str] = set()
                    ordered: list[str] = []
                    for phrase in (phrases if isinstance(phrases, list) else []):
                        if isinstance(phrase, str) and phrase.strip() and phrase.strip() not in seen:
                            seen.add(phrase.strip())
                            ordered.append(phrase.strip())
                    key_phrases_list[i] = tuple(sorted(ordered[:max_key_phrases]))
                    statuses[i]["key_phrases"] = ModuleStatus.SUCCEEDED
        except Exception:
            logger.warning("Key phrases enrichment failed for batch", exc_info=True)
            for i in range(len(texts)):
                statuses[i]["key_phrases"] = ModuleStatus.FAILED

    if entities_enabled:
        try:
            results = client.recognize_entities(documents)
            for i, result in enumerate(results):
                if getattr(result, "is_error", False):
                    statuses[i]["entities"] = ModuleStatus.FAILED
                else:
                    raw_entities = getattr(result, "entities", [])
                    seen_keys: set[tuple[str, str, str]] = set()
                    parsed: list[Entity] = []
                    for entity in raw_entities:
                        text = getattr(entity, "text", None)
                        category = getattr(entity, "category", None)
                        subcategory = getattr(entity, "subcategory", None)
                        confidence = getattr(entity, "confidence_score", None)
                        if not isinstance(text, str) or not text.strip() or not isinstance(category, str):
                            continue
                        key = (text.strip(), category, subcategory or "")
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        parsed.append(Entity(
                            text=text.strip(),
                            category=category,
                            subcategory=subcategory if isinstance(subcategory, str) and subcategory else None,
                            confidence=confidence if isinstance(confidence, (int, float)) and 0 <= confidence <= 1 else None,
                        ))
                    entities_list[i] = tuple(sorted(parsed[:max_entities], key=lambda e: (e.category, e.text)))
                    statuses[i]["entities"] = ModuleStatus.SUCCEEDED
        except Exception:
            logger.warning("Entity enrichment failed for batch", exc_info=True)
            for i in range(len(texts)):
                statuses[i]["entities"] = ModuleStatus.FAILED

    for i in range(len(texts)):
        batch_results.append({
            "summary": summaries[i],
            "key_phrases": key_phrases_list[i],
            "entities": entities_list[i],
            "status": EnrichmentStatuses(
                summary=statuses[i]["summary"],
                key_phrases=statuses[i]["key_phrases"],
                entities=statuses[i]["entities"],
            ),
        })
    return batch_results


def _skipped_enrichment() -> dict[str, Any]:
    return {
        "summary": None,
        "key_phrases": (),
        "entities": (),
        "status": EnrichmentStatuses(
            summary=ModuleStatus.NOT_REQUESTED,
            key_phrases=ModuleStatus.NOT_REQUESTED,
            entities=ModuleStatus.NOT_REQUESTED,
        ),
    }
