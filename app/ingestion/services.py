"""Ingestion business logic: activate, discover, process, finalize."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from config import IngestionConfig
from ingestion.chunking import chunk_pages, token_count
from ingestion.embedding import embed_texts
from ingestion.enrichment import enrich_chunks
from ingestion.errors import TerminalDocumentError
from ingestion.extraction import extract_pdf
from ingestion.graph import (
    DeltaResetRequired,
    DiscoveryState,
    DiscoveryStep,
    VerifiedAcl,
    discovered_pdf_from_item,
)
from ingestion.lifecycle_repository import (
    DocumentLifecycleRepository,
    LifecycleConflictError,
    LifecycleDocumentRef,
    ReadyDocumentRef,
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
    RETIRED_REASONS,
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
from ingestion.source_connector import SourceConnector
from ingestion.telemetry import write_audit_record

logger = logging.getLogger(__name__)


def activate(config: IngestionConfig, repository: IngestionRepository, orchestration_instance_id: str) -> ActivatedRun:
    """Create a new run and atomically update currentRunId."""
    now = _utc_now()
    run_id = create_run_id(now, config.source_id)
    instance_id = orchestration_instance_id
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
    connector: SourceConnector,
) -> tuple[list[SourceDocumentRecord], int]:
    """Discover all eligible files from the source and persist as documents."""
    settings = Settings(source_id=config.source_id, drive_id=config.drive_id, code_version="durable-sync")
    state = DiscoveryState.initial()
    documents: list[SourceDocumentRecord] = []
    skipped = 0

    # Check previous run for skip-if-ready optimization
    control = repository.get_source_control(config.source_id)
    prev_run_id = control.record.last_completed_run_id if control else None
    prev_source_run_id = create_source_run_id(config.source_id, prev_run_id) if prev_run_id else None

    while not state.complete:
        step = connector.discover_next_page(
            state, settings.limits, allowed_extensions=config.allowed_extensions,
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
                    discovered_modified = getattr(pdf, "last_modified_date_time", None)
                    if discovered_modified is None or prev.record.source_modified_at == discovered_modified:
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
    lifecycle_repository: DocumentLifecycleRepository,
    connector: SourceConnector,
    di_client: Any | None,
    language_client: Any | None,
    openai_client: Any,
    audit_container: Any | None = None,
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
        acl = connector.read_verified_acl(current_doc.item_id, config.acl_max_pages)

        content = connector.download_content_sync(current_doc.item_id, ScaleLimits().max_pdf_bytes, config.download_timeout_seconds)

        if config.extraction_enabled and di_client is not None:
            extraction_start = time.perf_counter()
            pages = extract_pdf(di_client, content, max_pdf_pages=config.max_pdf_pages)
            if audit_container is not None:
                total_chars = sum(len(p.text) for p in pages)
                write_audit_record(audit_container, config.source_id, current_doc.source_run_id, {
                    "operation": "document_extraction", "model": "prebuilt-layout",
                    "pages": len(pages), "characters": total_chars,
                    "latency_ms": int((time.perf_counter() - extraction_start) * 1000),
                    "documentId": current_doc.document_id, "sourceName": current_doc.source_name,
                })
        else:
            raise TerminalDocumentError("extraction_disabled_no_alternative")

        chunks = chunk_pages(pages)

        if config.enrichment_enabled and language_client is not None:
            enrichment_start = time.perf_counter()
            enrichments = enrich_chunks(
                language_client, [chunk.content for chunk in chunks],
                summary_enabled=config.summary_enabled,
                key_phrases_enabled=config.key_phrases_enabled,
                entities_enabled=config.entities_enabled,
            )
            if audit_container is not None:
                statuses = [e["status"] for e in enrichments]
                write_audit_record(audit_container, config.source_id, current_doc.source_run_id, {
                    "operation": "enrichment", "chunks": len(chunks),
                    "key_phrases": "succeeded" if any(s.key_phrases.value == "succeeded" for s in statuses) else "failed",
                    "entities": "succeeded" if any(s.entities.value == "succeeded" for s in statuses) else "failed",
                    "summary": "succeeded" if config.summary_enabled and any(s.summary.value == "succeeded" for s in statuses) else "not_requested",
                    "latency_ms": int((time.perf_counter() - enrichment_start) * 1000),
                })
        else:
            enrichments = enrich_chunks(None, [chunk.content for chunk in chunks])

        searchable_texts = [
            _build_searchable_text(chunk.content, enrichments[i]["key_phrases"], enrichments[i]["summary"])
            for i, chunk in enumerate(chunks)
        ]

        embeddings = embed_texts(
            openai_client, searchable_texts,
            audit_container=audit_container,
            source_id=config.source_id,
            run_id=current_doc.source_run_id,
            batch_size=config.embedding_batch_size,
        )

        now = _fmt(_utc_now())
        chunk_records = _build_chunk_records(current_doc, acl, chunks, searchable_texts, enrichments, embeddings, now)

        current_doc = replace(current_doc, stage=DocumentStage.PERSISTING, page_count=len(pages), expected_chunk_count=len(chunk_records), content_hash=content_sha256("\n".join(p.text for p in pages)), extraction_mode="prebuilt-layout", updated_at=_fmt(_utc_now()))
        updated = repository.update_processing_document(current_doc, current_etag)
        current_doc, current_etag = updated.record, updated.etag

        written = repository.write_chunks(chunk_records)

        admitting_doc = replace(
            current_doc,
            status=DocumentStatus.ADMITTING,
            stage=DocumentStage.VERIFYING,
            allowed_group_ids=acl.allowed_group_ids,
            acl_hash=acl.acl_hash,
            acl_evaluated_at=_fmt(_utc_now()),
            expected_chunk_count=len(chunk_records),
            written_chunk_count=written,
            updated_at=_fmt(_utc_now()),
        )
        admitting = repository.begin_document_admission(admitting_doc, current_etag)
        current_doc, current_etag = admitting.record, admitting.etag

        lifecycle_repository.set_document_chunks_retrievable(
            document_key=current_doc.document_key,
            lifecycle_generation=current_doc.lifecycle_generation,
            is_retrievable=True,
            allowed_group_ids=acl.allowed_group_ids,
            expected_count=written,
        )

        ready_doc = replace(
            current_doc,
            status=DocumentStatus.READY, stage=DocumentStage.TERMINAL,
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
    counters = repository.compute_run_counters(
        config.source_id, current.record.run_id, retries=0, items_scanned=items_scanned,
    )
    status = RunStatus.COMPLETED_WITH_ERRORS if counters.failed else RunStatus.COMPLETED
    terminal_run = replace(
        current.record,
        status=status,
        stage=RunStage.TERMINAL,
        completed_at=_fmt(_utc_now()),
        updated_at=_fmt(_utc_now()),
    )
    return repository.finalize_run(terminal_run, run_etag, retries=0, items_scanned=items_scanned)


def terminate_run(
    config: IngestionConfig,
    repository: IngestionRepository,
) -> dict[str, Any]:
    """Fail all non-terminal docs and finalize the run as TERMINATED."""
    control = repository.get_source_control(config.source_id)
    if control is None:
        return {"status": "no_active_run"}
    current = repository.get_run(config.source_id, control.record.current_run_id)
    if current is None:
        return {"status": "no_active_run"}
    if current.record.stage is RunStage.TERMINAL:
        return {"status": "already_terminal", "runStatus": current.record.status.value}

    failed_count = repository.fail_nonterminal_documents(
        config.source_id, current.record.run_id, "orchestration_terminated",
    )
    terminal_run = replace(
        current.record,
        status=RunStatus.TERMINATED,
        stage=RunStage.TERMINAL,
        completed_at=_fmt(_utc_now()),
        updated_at=_fmt(_utc_now()),
    )
    finalized = repository.finalize_run(terminal_run, current.etag, retries=0, items_scanned=0)
    return {
        "status": "terminated",
        "runId": current.record.run_id,
        "docsForceFailed": failed_count,
        "counters": finalized.record.counters.__dict__ if hasattr(finalized.record.counters, '__dict__') else {},
    }


def get_retry_candidates(
    config: IngestionConfig,
    repository: IngestionRepository,
) -> list[dict[str, Any]]:
    """Return failed documents from the current run that can be retried."""
    control = repository.get_source_control(config.source_id)
    if control is None:
        return []
    run_id = control.record.current_run_id
    if not run_id:
        return []
    return repository.get_failed_documents(config.source_id, run_id)


@dataclass(frozen=True)
class DeltaSyncOutcome:
    bootstrapped: bool = False
    created_or_updated: int = 0
    deleted: int = 0
    acl_resynced: int = 0
    failed: int = 0
    items_seen: int = 0


def run_delta_sync(
    config: IngestionConfig,
    repository: IngestionRepository,
    lifecycle_repository: DocumentLifecycleRepository,
    connector: SourceConnector,
    di_client: Any | None,
    language_client: Any | None,
    openai_client: Any,
    audit_container: Any | None = None,
) -> DeltaSyncOutcome:
    """One delta-sync tick: process adds/updates/deletes for source_id since the last
    cursor. Uses its own run_id per tick purely as a schema-compliant namespacing device
    (sourceRunId/documentKey); it does not touch full-sync's source-control singleton."""
    cursor = lifecycle_repository.get_delta_cursor(config.source_id)
    if cursor is None:
        bootstrap_link = connector.bootstrap_delta_cursor()
        lifecycle_repository.save_delta_cursor(config.source_id, bootstrap_link)
        return DeltaSyncOutcome(bootstrapped=True)

    try:
        delta = connector.read_drive_delta(config.delta_max_pages, delta_link=cursor)
    except DeltaResetRequired:
        # Cursor permanently invalidated; re-bootstrap per MS Graph guidance
        logger.warning("delta_cursor_invalidated, re-bootstrapping")
        bootstrap_link = connector.bootstrap_delta_cursor()
        lifecycle_repository.save_delta_cursor(config.source_id, bootstrap_link)
        return DeltaSyncOutcome(bootstrapped=True)
    run_id = create_run_id(_utc_now(), f"{config.source_id}:delta")

    created_or_updated = 0
    deleted = 0
    acl_resynced = 0
    failed = 0
    for ordinal, item in enumerate(delta.items):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            failed += 1
            continue
        document_id = create_document_id(config.source_id, config.drive_id, item_id)

        if item.get("deleted") is not None:
            try:
                ref = lifecycle_repository.find_ready_document_by_document_id(document_id)
                if ref is not None:
                    try:
                        lifecycle_repository.delete_document_and_chunks(
                            source_run_id=ref.source_run_id,
                            document_id=document_id,
                            document_key=ref.document_key,
                            etag=ref.etag,
                        )
                    except LifecycleConflictError:
                        pass  # already deleted/changed concurrently -- acceptable no-op
                    else:
                        deleted += 1
                        if audit_container is not None:
                            write_audit_record(audit_container, config.source_id, run_id, {
                                "operation": "document_deleted", "documentId": document_id,
                                "reason": "deleted",
                                "sourceName": getattr(ref, "source_name", ""),
                                "sourceUrl": getattr(ref, "source_url", ""),
                                "method": "delta_sync",
                            })
            except Exception:
                logger.error("delta_sync delete failed for item %s", item_id, exc_info=True)
                failed += 1
            continue

        # Per-item permission change: Graph only surfaces this for file-level ACL edits;
        # library-level permission changes are caught by the zero-delta ACL resync path.
        if item.get("@microsoft.graph.sharedChanged") is True:
            ref = lifecycle_repository.find_ready_document_by_document_id(document_id)
            if ref is None:
                ref = lifecycle_repository.find_acl_revoked_document_by_document_id(
                    document_id
                )
            if ref is None:
                continue
            try:
                old_groups = list(ref.allowed_group_ids)
                result = resync_document_acl(config, ref, lifecycle_repository, connector)
                acl_resynced += 1
                if audit_container is not None:
                    write_audit_record(audit_container, config.source_id, run_id, {
                        "operation": "acl_resynced", "documentId": document_id,
                        "result": result, "method": "delta_sync",
                        "previousGroupIds": old_groups,
                    })
            except Exception:
                logger.error("delta_sync acl_resync failed for item %s", item_id, exc_info=True)
                failed += 1
                continue
            if result == "retired" or not _source_etag_changed(item, ref.source_etag):
                continue

        try:
            document = _delta_item_to_document(item, config, run_id, ordinal)
            if document is None:
                continue
            prev_ref = lifecycle_repository.find_ready_document_by_document_id(document_id)
            stored = repository.create_discovered_document(document)
            outcome = process_document(
                config, stored.record, stored.etag, repository, lifecycle_repository,
                connector, di_client, language_client, openai_client,
                audit_container=audit_container,
            )
        except Exception:
            logger.error("delta_sync item %s failed", item_id, exc_info=True)
            failed += 1
            continue

        if outcome.status is not ActivityStatus.SUCCEEDED:
            failed += 1
            continue
        created_or_updated += 1
        if audit_container is not None:
            write_audit_record(audit_container, config.source_id, run_id, {
                "operation": "document_ingested", "documentId": document_id,
                "sourceName": document.source_name, "sourceUrl": document.source_url,
                "method": "delta_sync", "action": "updated" if prev_ref else "created",
                "chunks": outcome.chunks_written,
            })
        if prev_ref is not None and prev_ref.document_key != document.document_key:
            try:
                lifecycle_repository.delete_document_and_chunks(
                    source_run_id=prev_ref.source_run_id,
                    document_id=document_id,
                    document_key=prev_ref.document_key,
                    etag=prev_ref.etag,
                )
            except LifecycleConflictError:
                pass  # already deleted/changed concurrently (e.g. ACL resync) -- acceptable no-op
            else:
                if audit_container is not None:
                    write_audit_record(audit_container, config.source_id, run_id, {
                        "operation": "document_deleted", "documentId": document_id,
                        "reason": "superseded", "method": "delta_sync",
                        "replacedDocumentKey": prev_ref.document_key,
                        "newDocumentKey": document.document_key,
                        "sourceName": document.source_name,
                    })

    if failed == 0:
        lifecycle_repository.save_delta_cursor(config.source_id, delta.delta_link)
    return DeltaSyncOutcome(
        created_or_updated=created_or_updated,
        deleted=deleted,
        acl_resynced=acl_resynced,
        failed=failed,
        items_seen=len(delta.items),
    )


def _delta_item_to_document(
    item: dict[str, Any], config: IngestionConfig, run_id: str, ordinal: int
) -> SourceDocumentRecord | None:
    """Adapt one Graph delta driveItem into a DISCOVERED document shell, or None if the
    item is a folder/package or doesn't match the configured file extensions."""
    if item.get("folder") is not None or item.get("package") is not None:
        return None
    if item.get("file") is None:
        return None
    name = item.get("name")
    if not isinstance(name, str) or not any(
        name.lower().endswith(ext) for ext in config.allowed_extensions
    ):
        return None
    pdf = discovered_pdf_from_item(item, ordinal)
    return _pdf_to_document(pdf, config, run_id, ingestion_mode="delta-sync")


def _source_etag_changed(item: dict[str, Any], persisted_etag: str | None) -> bool:
    source_etag = item.get("eTag")
    return isinstance(source_etag, str) and bool(source_etag) and source_etag != persisted_etag


@dataclass(frozen=True)
class AclResyncOutcome:
    checked: int = 0
    unchanged: int = 0
    updated: int = 0
    retired: int = 0


@dataclass(frozen=True)
class LifecycleReconciliationOutcome:
    checked: int = 0
    repaired: int = 0
    failed: int = 0


def run_lifecycle_reconciliation_page(
    repository: IngestionRepository,
    lifecycle_repository: DocumentLifecycleRepository,
    *,
    page_size: int,
    continuation_token: str | None = None,
) -> tuple[LifecycleReconciliationOutcome, str | None]:
    page = lifecycle_repository.list_lifecycle_transitions_page(
        page_size=page_size,
        continuation_token=continuation_token,
    )
    repaired = 0
    failed = 0
    for ref in page.items:
        try:
            _reconcile_lifecycle_document(repository, lifecycle_repository, ref)
            repaired += 1
        except Exception:
            failed += 1
            logger.warning(
                "Lifecycle reconciliation failed for document %s",
                ref.document_id,
                exc_info=True,
            )
    return (
        LifecycleReconciliationOutcome(
            checked=len(page.items),
            repaired=repaired,
            failed=failed,
        ),
        page.continuation_token,
    )


def run_duplicate_version_reconciliation_page(
    config: IngestionConfig,
    repository: IngestionRepository,
    lifecycle_repository: DocumentLifecycleRepository,
    *,
    page_size: int,
    continuation_token: str | None = None,
) -> tuple[LifecycleReconciliationOutcome, str | None]:
    page = lifecycle_repository.list_duplicate_ready_document_ids_page(
        page_size=page_size,
        continuation_token=continuation_token,
    )
    control = repository.get_source_control(config.source_id)
    current_source_run_id = (
        create_source_run_id(config.source_id, control.record.current_run_id)
        if control is not None
        else None
    )
    repaired = 0
    failed = 0
    for document_id in page.document_ids:
        try:
            versions = lifecycle_repository.list_ready_document_versions(document_id)
            if current_source_run_id is None or not any(
                ref.source_run_id == current_source_run_id for ref in versions
            ):
                continue
            for ref in versions:
                if ref.source_run_id == current_source_run_id:
                    continue
                lifecycle_repository.delete_document_and_chunks(
                    source_run_id=ref.source_run_id,
                    document_id=ref.document_id,
                    document_key=ref.document_key,
                    etag=ref.etag,
                )
                repaired += 1
        except Exception:
            failed += 1
            logger.warning(
                "Duplicate-version reconciliation failed for document %s",
                document_id,
                exc_info=True,
            )
    return (
        LifecycleReconciliationOutcome(
            checked=len(page.document_ids),
            repaired=repaired,
            failed=failed,
        ),
        page.continuation_token,
    )


def run_orphan_chunk_reconciliation_page(
    repository: IngestionRepository,
    lifecycle_repository: DocumentLifecycleRepository,
    *,
    page_size: int,
    continuation_token: str | None = None,
) -> tuple[LifecycleReconciliationOutcome, str | None]:
    page = lifecycle_repository.list_chunk_manifest_refs_page(
        page_size=page_size,
        continuation_token=continuation_token,
    )
    repaired = 0
    failed = 0
    for ref in page.items:
        try:
            manifest = repository.get_document(ref.source_run_id, ref.document_id)
            if manifest is None:
                lifecycle_repository.delete_orphan_chunk(
                    chunk_id=ref.chunk_id,
                    document_key=ref.document_key,
                )
                repaired += 1
        except Exception:
            failed += 1
            logger.warning(
                "Orphan-chunk reconciliation failed for chunk %s",
                ref.chunk_id,
                exc_info=True,
            )
    return (
        LifecycleReconciliationOutcome(
            checked=len(page.items),
            repaired=repaired,
            failed=failed,
        ),
        page.continuation_token,
    )


def _reconcile_lifecycle_document(
    repository: IngestionRepository,
    lifecycle_repository: DocumentLifecycleRepository,
    ref: LifecycleDocumentRef,
) -> None:
    if ref.status is DocumentStatus.ADMITTING:
        stored = repository.get_document(ref.source_run_id, ref.document_id)
        if stored is None or stored.record.status is not DocumentStatus.ADMITTING:
            raise RepositoryConflictError("admitting document changed during reconciliation")
        expected_count = stored.record.expected_chunk_count
        if expected_count is None:
            raise RepositoryConflictError("admitting document has no expected chunk count")
        lifecycle_repository.set_document_chunks_retrievable(
            document_key=stored.record.document_key,
            lifecycle_generation=stored.record.lifecycle_generation,
            is_retrievable=True,
            allowed_group_ids=stored.record.allowed_group_ids,
            expected_count=expected_count,
        )
        repository.verify_and_mark_document_ready(
            replace(
                stored.record,
                status=DocumentStatus.READY,
                stage=DocumentStage.TERMINAL,
                ready_at=_fmt(_utc_now()),
                updated_at=_fmt(_utc_now()),
            ),
            stored.etag,
        )
        return
    if ref.status is DocumentStatus.ACL_REFRESHING:
        if ref.pending_allowed_group_ids is None or not ref.pending_acl_hash:
            raise RepositoryConflictError("ACL refresh intent is incomplete")
        lifecycle_repository.refresh_document_acl(
            source_run_id=ref.source_run_id,
            document_id=ref.document_id,
            document_key=ref.document_key,
            etag=ref.etag,
            allowed_group_ids=ref.pending_allowed_group_ids,
            acl_hash=ref.pending_acl_hash,
        )
        return
    if ref.status is DocumentStatus.RETIRING:
        if ref.pending_retired_reason not in RETIRED_REASONS:
            raise RepositoryConflictError("retirement intent is incomplete")
        lifecycle_repository.retire_document(
            source_run_id=ref.source_run_id,
            document_id=ref.document_id,
            document_key=ref.document_key,
            etag=ref.etag,
            reason=ref.pending_retired_reason,
        )
        return
    if ref.status is DocumentStatus.DELETING:
        lifecycle_repository.delete_document_and_chunks(
            source_run_id=ref.source_run_id,
            document_id=ref.document_id,
            document_key=ref.document_key,
            etag=ref.etag,
        )
        return
    raise RepositoryConflictError("unsupported lifecycle transition")


def resync_document_acl(
    config: IngestionConfig,
    ref: ReadyDocumentRef,
    lifecycle_repository: DocumentLifecycleRepository,
    connector: SourceConnector,
    audit_container: Any | None = None,
) -> str:
    """Re-verify one ACL-eligible document. Returns unchanged, updated, or retired."""
    try:
        acl = connector.read_verified_acl(ref.item_id, config.acl_max_pages)
    except TerminalDocumentError:
        if ref.status is DocumentStatus.RETIRED:
            return "unchanged"
        try:
            lifecycle_repository.retire_document(
                source_run_id=ref.source_run_id,
                document_id=ref.document_id,
                document_key=ref.document_key,
                etag=ref.etag,
                reason="acl_revoked",
            )
        except LifecycleConflictError:
            pass
        else:
            if audit_container is not None:
                write_audit_record(audit_container, config.source_id, ref.source_run_id, {
                    "operation": "document_retired", "documentId": ref.document_id,
                    "retiredReason": "acl_revoked",
                    "sourceName": getattr(ref, "source_name", ""),
                    "sourceUrl": getattr(ref, "source_url", ""),
                })
        return "retired"

    if acl.acl_hash == ref.acl_hash and ref.status is DocumentStatus.READY:
        return "unchanged"
    if ref.status is DocumentStatus.RETIRED:
        source_item = connector.read_item(ref.item_id)
        source_etag = source_item.get("eTag") if source_item is not None else None
        if not isinstance(source_etag, str) or source_etag != ref.source_etag:
            return "unchanged"
        if not lifecycle_repository.is_authoritative_document_version(
            document_id=ref.document_id,
            source_run_id=ref.source_run_id,
            source_etag=source_etag,
        ):
            return "unchanged"
    try:
        lifecycle_repository.refresh_document_acl(
            source_run_id=ref.source_run_id,
            document_id=ref.document_id,
            document_key=ref.document_key,
            etag=ref.etag,
            allowed_group_ids=acl.allowed_group_ids,
            acl_hash=acl.acl_hash,
        )
    except LifecycleConflictError:
        return "unchanged"
    return "updated"


def run_acl_resync_page(
    config: IngestionConfig,
    lifecycle_repository: DocumentLifecycleRepository,
    connector: SourceConnector,
    *,
    page_size: int,
    continuation_token: str | None,
    audit_container: Any | None = None,
) -> tuple[AclResyncOutcome, str | None]:
    """Re-verify ACLs for one bounded page of ready documents (Durable-activity-sized)."""
    page = lifecycle_repository.list_ready_documents_page(
        page_size=page_size, continuation_token=continuation_token
    )
    unchanged = updated = retired = 0
    for ref in page.items:
        result = resync_document_acl(config, ref, lifecycle_repository, connector, audit_container=audit_container)
        if result == "unchanged":
            unchanged += 1
        elif result == "updated":
            updated += 1
        else:
            retired += 1
    outcome = AclResyncOutcome(
        checked=len(page.items), unchanged=unchanged, updated=updated, retired=retired
    )
    return outcome, page.continuation_token


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


def _build_searchable_text(content: str, key_phrases: tuple[str, ...], summary: str | None) -> str:
    """Concatenate content with enrichment data for embedding and full-text search."""
    parts = [content]
    if key_phrases:
        parts.append("Key terms: " + ", ".join(key_phrases))
    if summary:
        parts.append("Summary: " + summary)
    return "\n\n".join(parts)


def _pdf_to_document(pdf: Any, config: IngestionConfig, run_id: str, ingestion_mode: str = "full-sync") -> SourceDocumentRecord:
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
        source_modified_at=getattr(pdf, "last_modified_date_time", None),
        ingestion_mode=ingestion_mode,
        id=document_id, document_id=document_id,
        source_run_id=create_source_run_id(config.source_id, run_id),
        document_key=create_document_key(config.source_id, run_id, document_id),
    )


def _build_chunk_records(
    document: SourceDocumentRecord, acl: Any, chunks: list[Chunk],
    searchable_texts: list[str], enrichments: list[dict], embeddings: list[tuple[float, ...]],
    now: str,
) -> tuple[SearchChunkRecord, ...]:
    records: list[SearchChunkRecord] = []
    for i, chunk in enumerate(chunks):
        enrichment = enrichments[i]
        searchable = _build_searchable_text(chunk.content, enrichment["key_phrases"], enrichment["summary"])
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
            embedding_text=searchable,
            searchable_text=searchable,
            token_count=token_count(chunk.content),
            enrichment_status=enrichment["status"],
            summary=enrichment["summary"],
            key_phrases=enrichment["key_phrases"],
            entities=enrichment["entities"],
            language_code="en",
            embedding=embeddings[i],
            embedded_at=now,
            source_modified_at=document.source_modified_at,
            is_retrievable=False,
            lifecycle_generation=document.lifecycle_generation,
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
