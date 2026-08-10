"""Ingestion business logic: activate, discover, process, finalize."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import httpx

from config import IngestionConfig
from ingestion.chunking import chunk_pages, token_count
from ingestion.embedding import embed_texts
from ingestion.enrichment import enrich_chunks
from ingestion.errors import TerminalDocumentError
from ingestion.extraction import extract_pdf
from ingestion.graph import (
    DiscoveryState,
    DiscoveryStep,
    discover_next_page,
    download_content_sync,
    read_verified_acl,
)
from ingestion.models import (
    ActivityOutcome,
    ActivityStatus,
    Chunk,
    DocumentStage,
    DocumentStatus,
    EnrichmentProfile,
    IngestionRunRecord,
    ProfileSnapshot,
    RunCounters,
    RunStage,
    RunStatus,
    SafeError,
    ScaleLimits,
    SearchChunkRecord,
    Settings,
    SourceControlRecord,
    SourceDocumentRecord,
    content_sha256,
    create_chunk_id,
    create_document_id,
    create_document_key,
    create_orchestration_instance_id,
    create_run_id,
    create_source_run_id,
    run_record_id,
    safe_error_from_exception,
)
from ingestion.repository import (
    ActivatedRun,
    IngestionRepository,
    RepositoryConflictError,
    VersionedRecord,
)

logger = logging.getLogger(__name__)

ACL_MAX_PAGES = 10
DOWNLOAD_TIMEOUT_SECONDS = 120.0


def activate(config: IngestionConfig, repository: IngestionRepository) -> ActivatedRun:
    """Create a new run and atomically update currentRunId."""
    now = _utc_now()
    run_id = create_run_id(now, config.source_id)
    instance_id = create_orchestration_instance_id(config.source_id)
    run = IngestionRunRecord(
        source_id=config.source_id,
        run_id=run_id,
        drive_id=config.drive_id,
        orchestration_instance_id=instance_id,
        status=RunStatus.RUNNING,
        stage=RunStage.ACTIVATING,
        started_at=_fmt(now),
        activated_at=_fmt(now),
        updated_at=_fmt(now),
        counters=RunCounters(),
        profiles=ProfileSnapshot(
            enrichment=EnrichmentProfile(
                summary_enabled=config.summary_enabled,
                key_phrases_enabled=config.key_phrases_enabled,
                entities_enabled=config.entities_enabled,
            ),
        ),
        ingestion_mode="full-sync",
        id=run_record_id(run_id),
    )
    control = SourceControlRecord(
        source_id=config.source_id,
        current_run_id=run_id,
        current_orchestration_instance_id=instance_id,
        activated_at=_fmt(now),
        updated_at=_fmt(now),
    )
    return repository.activate_run(run, control)


def discover_all(
    config: IngestionConfig,
    run_id: str,
    repository: IngestionRepository,
    graph_client: httpx.Client,
) -> tuple[list[SourceDocumentRecord], int]:
    """Discover all eligible files from SharePoint and persist as documents."""
    settings = Settings(source_id=config.source_id, drive_id=config.drive_id, code_version="durable-sync")
    state = DiscoveryState.initial()
    documents: list[SourceDocumentRecord] = []
    skipped = 0

    # Check previous run for skip-if-ready optimization
    control = repository.get_source_control(config.source_id)
    prev_run_id = control.record.last_completed_run_id if control else None
    prev_source_run_id = create_source_run_id(config.source_id, prev_run_id) if prev_run_id else None

    while not state.complete:
        step = discover_next_page(
            graph_client, config.drive_id, state, settings.limits,
            allowed_extensions=config.allowed_extensions,
        )
        state = step.state
        for pdf in step.pdfs:
            doc_id = create_document_id(config.source_id, config.drive_id, pdf.item_id)
            if prev_source_run_id:
                try:
                    prev = repository.get_document(prev_source_run_id, doc_id)
                except Exception:
                    logger.warning("Skip-if-ready lookup failed for %s", doc_id, exc_info=True)
                    prev = None
                if prev and prev.record.status is DocumentStatus.READY and prev.record.e_tag == pdf.e_tag:
                    skipped += 1
                    continue
            doc = _pdf_to_document(pdf, config, run_id)
            stored = repository.create_discovered_document(doc)
            documents.append(stored.record)

    logger.info("Discovery complete: %d to process, %d skipped (unchanged), %d items scanned", len(documents), skipped, state.items_scanned)
    return documents, state.items_scanned


def process_document(
    config: IngestionConfig,
    document: SourceDocumentRecord,
    document_etag: str,
    repository: IngestionRepository,
    graph_client: httpx.Client,
    di_client: Any | None,
    language_client: Any | None,
    openai_client: Any,
) -> ActivityOutcome:
    """Process a single document through the full pipeline."""
    try:
        processing = repository.mark_document_processing(
            replace(
                document,
                status=DocumentStatus.PROCESSING,
                stage=DocumentStage.ACL,
                attempt_count=document.attempt_count + 1,
                processing_started_at=_fmt(_utc_now()),
                updated_at=_fmt(_utc_now()),
            ),
            document_etag,
        )
    except RepositoryConflictError:
        return ActivityOutcome(document_id=document.document_id, status=ActivityStatus.SKIPPED, chunks_written=0, retry_count=0)

    current_doc = processing.record
    current_etag = processing.etag
    try:
        acl = read_verified_acl(graph_client, config.drive_id, current_doc.item_id, ACL_MAX_PAGES)

        content = download_content_sync(graph_client, config.drive_id, current_doc.item_id, ScaleLimits().max_pdf_bytes, DOWNLOAD_TIMEOUT_SECONDS)

        if config.extraction_enabled and di_client is not None:
            pages = extract_pdf(di_client, content)
        else:
            raise TerminalDocumentError("extraction_disabled_no_alternative")

        chunks = chunk_pages(pages)
        cleaned_texts = [_clean_text(chunk.content) for chunk in chunks]

        if config.enrichment_enabled and language_client is not None:
            enrichments = enrich_chunks(
                language_client, [chunk.content for chunk in chunks],
                summary_enabled=config.summary_enabled,
                key_phrases_enabled=config.key_phrases_enabled,
                entities_enabled=config.entities_enabled,
            )
        else:
            enrichments = enrich_chunks(None, [chunk.content for chunk in chunks])

        embeddings = embed_texts(openai_client, cleaned_texts)

        now = _fmt(_utc_now())
        chunk_records = _build_chunk_records(current_doc, acl, chunks, cleaned_texts, enrichments, embeddings, now)

        current_doc = replace(current_doc, stage=DocumentStage.PERSISTING, page_count=len(pages), expected_chunk_count=len(chunk_records), content_hash=content_sha256("\n".join(p.text for p in pages)), extraction_mode="prebuilt-layout", updated_at=_fmt(_utc_now()))
        updated = repository.update_processing_document(current_doc, current_etag)
        current_doc, current_etag = updated.record, updated.etag

        written = repository.write_chunks(chunk_records)

        # Mark ready
        ready_doc = replace(
            current_doc,
            status=DocumentStatus.READY, stage=DocumentStage.TERMINAL,
            allowed_group_ids=acl.allowed_group_ids, acl_hash=acl.acl_hash,
            acl_evaluated_at=_fmt(_utc_now()), expected_chunk_count=len(chunk_records),
            written_chunk_count=written,
            ready_at=_fmt(_utc_now()), updated_at=_fmt(_utc_now()),
        )
        repository.verify_and_mark_document_ready(ready_doc, current_etag)

        return ActivityOutcome(document_id=document.document_id, status=ActivityStatus.SUCCEEDED, chunks_written=written, retry_count=document.attempt_count)

    except TerminalDocumentError as error:
        _fail_document(current_doc, current_etag, error, repository)
        return ActivityOutcome(document_id=document.document_id, status=ActivityStatus.FAILED, chunks_written=0, retry_count=document.attempt_count, error=SafeError(str(error), current_doc.stage.value, False))
    except RepositoryConflictError:
        return ActivityOutcome(document_id=document.document_id, status=ActivityStatus.SKIPPED, chunks_written=0, retry_count=document.attempt_count)
    except Exception as error:
        logger.error("Document %s failed at stage=%s: %s", document.document_id, current_doc.stage.value, error, exc_info=True)
        safe = safe_error_from_exception(error, current_doc.stage.value)
        if safe.retryable:
            raise
        _fail_document(current_doc, current_etag, error, repository)
        return ActivityOutcome(document_id=document.document_id, status=ActivityStatus.FAILED, chunks_written=0, retry_count=document.attempt_count, error=safe)


def finalize(
    config: IngestionConfig,
    run_etag: str,
    repository: IngestionRepository,
    items_scanned: int,
) -> VersionedRecord[IngestionRunRecord]:
    """Compute exact counters and mark run terminal."""
    control = repository.get_source_control(config.source_id)
    if control is None:
        raise RepositoryConflictError("source control not found")
    current = repository.get_run(config.source_id, control.record.current_run_id)
    if current is None:
        raise RepositoryConflictError("run no longer exists")
    terminal_run = replace(
        current.record,
        status=RunStatus.COMPLETED,
        stage=RunStage.TERMINAL,
        completed_at=_fmt(_utc_now()),
        updated_at=_fmt(_utc_now()),
    )
    return repository.finalize_run(terminal_run, run_etag, retries=0, items_scanned=items_scanned)


# --- Private helpers ---

def _fail_document(doc: SourceDocumentRecord, etag: str, error: BaseException, repository: IngestionRepository) -> None:
    try:
        if isinstance(error, TerminalDocumentError):
            safe = SafeError(str(error), doc.stage.value, False)
        else:
            safe = safe_error_from_exception(error, doc.stage.value)
        failed = replace(doc, status=DocumentStatus.FAILED, stage=DocumentStage.TERMINAL, failed_at=_fmt(_utc_now()), updated_at=_fmt(_utc_now()), error=safe)
        repository.mark_document_failed(failed, etag)
    except Exception:
        logger.warning("Failed to mark document as failed", exc_info=True)


def _clean_text(text: str) -> str:
    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _pdf_to_document(pdf: Any, config: IngestionConfig, run_id: str) -> SourceDocumentRecord:
    document_id = create_document_id(config.source_id, config.drive_id, pdf.item_id)
    now = _fmt(_utc_now())
    return SourceDocumentRecord(
        source_id=config.source_id, run_id=run_id, drive_id=config.drive_id,
        item_id=pdf.item_id, parent_item_id=pdf.parent_item_id,
        source_name=pdf.name, source_path=pdf.source_path,
        source_url=pdf.source_url, e_tag=pdf.e_tag,
        mime_type="application/pdf", size_bytes=pdf.size_bytes,
        discovery_ordinal=pdf.discovery_ordinal,
        allowed_group_ids=("pending",), acl_hash=content_sha256("pending"),
        acl_evaluated_at=now,
        status=DocumentStatus.DISCOVERED, stage=DocumentStage.DISCOVERED,
        attempt_count=0, discovered_at=now, updated_at=now,
        id=document_id, document_id=document_id,
        source_run_id=create_source_run_id(config.source_id, run_id),
        document_key=create_document_key(config.source_id, run_id, document_id),
    )


def _build_chunk_records(
    document: SourceDocumentRecord, acl: Any, chunks: list[Chunk],
    cleaned_texts: list[str], enrichments: list[dict], embeddings: list[tuple[float, ...]],
    now: str,
) -> tuple[SearchChunkRecord, ...]:
    records: list[SearchChunkRecord] = []
    for i, chunk in enumerate(chunks):
        enrichment = enrichments[i]
        records.append(SearchChunkRecord(
            source_id=document.source_id, run_id=document.run_id,
            document_id=document.document_id, document_key=document.document_key,
            allowed_group_ids=acl.allowed_group_ids,
            source_name=document.source_name,
            source_url=document.source_url,
            page_start=chunk.page_number, page_end=chunk.page_number,
            section_path=_section_path(chunk.content), chunk_index=chunk.ordinal,
            created_at=now, content=chunk.content,
            content_hash=content_sha256(chunk.content),
            embedding_text=cleaned_texts[i],
            token_count=token_count(chunk.content),
            enrichment_status=enrichment["status"],
            summary=enrichment["summary"],
            key_phrases=enrichment["key_phrases"],
            entities=enrichment["entities"],
            language_code="en",
            embedding=embeddings[i],
            embedded_at=now,
            id=create_chunk_id(chunk.ordinal),
            source_run_id=create_source_run_id(document.source_id, document.run_id),
        ))
    return tuple(records)


def _section_path(content: str) -> tuple[str, ...]:
    headings: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                headings.append(heading[:200])
    return tuple(headings[:5])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
