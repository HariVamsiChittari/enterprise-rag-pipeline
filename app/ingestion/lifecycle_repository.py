"""Post-ingestion document lifecycle: ACL resync, retirement, and delta-sync's
delete/supersede paths (Goals 6b and 8).

Kept separate from ingestion.repository.IngestionRepository by design: that module is
contractually schema-v1 full-sync only (see
tests/ingestion/test_repository.py::test_repository_exposes_no_forbidden_api_or_terminology,
which forbids "patch_item", "delta", and related terms from its source). This module owns
every Cosmos Patch API call and the delta-sync cursor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from azure.core import MatchConditions

from ingestion.models import DocumentStatus, RETIRED_REASONS

logger = logging.getLogger(__name__)

DELTA_CONTROL_ID = "delta-control"
WEBHOOK_CONTROL_ID = "webhook-subscription"
DELTA_SYNC_TRIGGER_ID = "delta-sync-trigger"
ACL_RESYNC_TRIGGER_ID = "acl-resync-trigger"
MAX_PATCH_BATCH_OPERATIONS = 100
INTERNAL_PAGE_SIZE = 100


class LifecycleRepositoryError(RuntimeError):
    """Base error with a message safe to expose in logs."""


class LifecycleConflictError(LifecycleRepositoryError):
    """Raised when persisted state conflicts with the requested operation (e.g. already
    retired, or changed concurrently). Callers may treat this as an idempotent no-op."""


@dataclass(frozen=True)
class ReadyDocumentRef:
    """A minimal projection of a ready source-document row, for lifecycle scans."""

    document_id: str
    source_run_id: str
    document_key: str
    item_id: str
    allowed_group_ids: tuple[str, ...]
    acl_hash: str
    etag: str


@dataclass(frozen=True)
class ReadyDocumentPage:
    items: tuple[ReadyDocumentRef, ...]
    continuation_token: str | None


class DocumentLifecycleRepository:
    """Owns the ACL-resync scan, retire/ACL-patch/hard-delete writes, and the delta-sync
    cursor. Uses the same three Cosmos containers as IngestionRepository."""

    def __init__(self, ingestion_runs: Any, source_documents: Any, search_chunks: Any) -> None:
        self._ingestion_runs = ingestion_runs
        self._source_documents = source_documents
        self._search_chunks = search_chunks

    def list_ready_documents_page(
        self,
        *,
        page_size: int,
        continuation_token: str | None = None,
    ) -> ReadyDocumentPage:
        _validate_page_size(page_size)
        query = (
            "SELECT c.documentId, c.sourceRunId, c.documentKey, c.itemId, "
            "c.allowedGroupIds, c.aclHash, c._etag FROM c WHERE c.status = @status"
        )
        parameters = [{"name": "@status", "value": DocumentStatus.READY.value}]
        try:
            iterator = self._source_documents.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True,
                max_item_count=page_size,
            )
            pager = iterator.by_page(continuation_token)
            page = list(next(pager, []))
            token = pager.continuation_token
        except Exception:
            raise LifecycleRepositoryError("Cosmos ready-document scan failed") from None
        return ReadyDocumentPage(tuple(_ready_ref_from_row(row) for row in page), token)

    def find_ready_document_by_document_id(self, document_id: str) -> ReadyDocumentRef | None:
        query = (
            "SELECT c.documentId, c.sourceRunId, c.documentKey, c.itemId, "
            "c.allowedGroupIds, c.aclHash, c._etag FROM c "
            "WHERE c.status = @status AND c.documentId = @documentId"
        )
        parameters = [
            {"name": "@status", "value": DocumentStatus.READY.value},
            {"name": "@documentId", "value": document_id},
        ]
        try:
            rows = list(
                self._source_documents.query_items(
                    query=query,
                    parameters=parameters,
                    enable_cross_partition_query=True,
                    max_item_count=1,
                )
            )
        except Exception:
            raise LifecycleRepositoryError("Cosmos ready-document lookup failed") from None
        if not rows:
            return None
        return _ready_ref_from_row(rows[0])

    def retire_document(
        self,
        *,
        source_run_id: str,
        document_id: str,
        etag: str,
        reason: str,
    ) -> None:
        if reason not in RETIRED_REASONS:
            raise ValueError("retired_reason must be one of the supported values")
        now = _now_iso()
        patch_operations = [
            {"op": "set", "path": "/status", "value": DocumentStatus.RETIRED.value},
            {"op": "set", "path": "/retiredAt", "value": now},
            {"op": "set", "path": "/retiredReason", "value": reason},
            {"op": "set", "path": "/updatedAt", "value": now},
        ]
        try:
            self._source_documents.patch_item(
                item=document_id,
                partition_key=source_run_id,
                patch_operations=patch_operations,
                filter_predicate="from c where c.status = 'ready'",
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as error:
            if _error_status(error) in (404, 412):
                raise LifecycleConflictError("document already retired or changed") from None
            raise LifecycleRepositoryError("Cosmos document retirement failed") from None

    def refresh_document_acl(
        self,
        *,
        source_run_id: str,
        document_id: str,
        document_key: str,
        etag: str,
        allowed_group_ids: tuple[str, ...],
        acl_hash: str,
    ) -> None:
        now = _now_iso()
        doc_patch = [
            {"op": "set", "path": "/allowedGroupIds", "value": list(allowed_group_ids)},
            {"op": "set", "path": "/aclHash", "value": acl_hash},
            {"op": "set", "path": "/aclEvaluatedAt", "value": now},
            {"op": "set", "path": "/updatedAt", "value": now},
        ]
        try:
            self._source_documents.patch_item(
                item=document_id,
                partition_key=source_run_id,
                patch_operations=doc_patch,
                filter_predicate="from c where c.status = 'ready'",
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as error:
            if _error_status(error) in (404, 412):
                raise LifecycleConflictError("document already changed or missing") from None
            raise LifecycleRepositoryError("Cosmos document ACL patch failed") from None
        self._patch_chunk_acl(document_key, allowed_group_ids)

    def _patch_chunk_acl(self, document_key: str, allowed_group_ids: tuple[str, ...]) -> None:
        group_values = list(allowed_group_ids)
        batch: list[str] = []
        for chunk_id in self._list_chunk_ids(document_key):
            batch.append(chunk_id)
            if len(batch) == MAX_PATCH_BATCH_OPERATIONS:
                self._patch_chunk_batch(document_key, batch, group_values)
                batch = []
        if batch:
            self._patch_chunk_batch(document_key, batch, group_values)

    def _patch_chunk_batch(
        self, document_key: str, chunk_ids: list[str], group_values: list[str]
    ) -> None:
        operations = [
            (
                "patch",
                (chunk_id, [{"op": "set", "path": "/allowedGroupIds", "value": group_values}]),
            )
            for chunk_id in chunk_ids
        ]
        try:
            self._search_chunks.execute_item_batch(
                batch_operations=operations, partition_key=document_key
            )
        except Exception:
            raise LifecycleRepositoryError("Cosmos chunk ACL batch patch failed") from None

    def delete_document_and_chunks(
        self,
        *,
        source_run_id: str,
        document_id: str,
        document_key: str,
        etag: str,
    ) -> None:
        # Chunks are safe to delete unconditionally: each ingestion run mints a new
        # document_key, so a concurrent re-add can never collide with this document_key's chunks.
        for chunk_id in self._list_chunk_ids(document_key):
            self._delete_item(self._search_chunks, chunk_id, document_key)
        try:
            self._source_documents.delete_item(
                item=document_id,
                partition_key=source_run_id,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as error:
            if _error_status(error) == 404:
                return
            if _error_status(error) == 412:
                raise LifecycleConflictError("document already changed or removed") from None
            raise LifecycleRepositoryError("Cosmos document delete failed") from None

    def _list_chunk_ids(self, document_key: str) -> list[str]:
        query = "SELECT c.id FROM c WHERE c.documentKey = @documentKey"
        parameters = [{"name": "@documentKey", "value": document_key}]
        chunk_ids: list[str] = []
        continuation: str | None = None
        while True:
            try:
                iterator = self._search_chunks.query_items(
                    query=query,
                    parameters=parameters,
                    partition_key=document_key,
                    max_item_count=INTERNAL_PAGE_SIZE,
                )
                pager = iterator.by_page(continuation)
                page = list(next(pager, []))
                continuation = pager.continuation_token
            except Exception:
                raise LifecycleRepositoryError("Cosmos chunk id scan failed") from None
            chunk_ids.extend(row["id"] for row in page)
            if continuation is None:
                break
        return chunk_ids

    @staticmethod
    def _delete_item(container: Any, item_id: str, partition_key: str) -> None:
        try:
            container.delete_item(item=item_id, partition_key=partition_key)
        except Exception as error:
            if _error_status(error) == 404:
                return
            raise LifecycleRepositoryError("Cosmos delete failed") from None

    def get_delta_cursor(self, source_id: str) -> str | None:
        try:
            item = self._ingestion_runs.read_item(item=DELTA_CONTROL_ID, partition_key=source_id)
        except Exception as error:
            if _error_status(error) == 404:
                return None
            raise LifecycleRepositoryError("Cosmos delta cursor read failed") from None
        cursor = item.get("deltaLink")
        if cursor is not None and not isinstance(cursor, str):
            raise LifecycleRepositoryError("Cosmos delta cursor is malformed")
        return cursor

    def save_delta_cursor(self, source_id: str, delta_link: str) -> None:
        if not delta_link:
            raise ValueError("delta_link is required")
        item = {
            "id": DELTA_CONTROL_ID,
            "sourceId": source_id,
            "deltaLink": delta_link,
            "updatedAt": _now_iso(),
        }
        try:
            self._ingestion_runs.upsert_item(body=item)
        except Exception:
            raise LifecycleRepositoryError("Cosmos delta cursor save failed") from None

    def get_trigger_instance_id(self, source_id: str, control_id: str) -> str | None:
        """Last Durable instance ID dispatched for a periodic/webhook-triggered
        orchestration (control_id is DELTA_SYNC_TRIGGER_ID or ACL_RESYNC_TRIGGER_ID).

        Durable instance-ID reuse is best-effort/racy at the storage layer
        (Azure/azure-functions-durable-python#410), so callers must mint a fresh,
        never-reused ID per tick via save_trigger_instance_id rather than polling a
        fixed ID. This record only remembers which ID to poll for "still running".
        """
        try:
            item = self._ingestion_runs.read_item(item=control_id, partition_key=source_id)
        except Exception as error:
            if _error_status(error) == 404:
                return None
            raise LifecycleRepositoryError("Cosmos trigger-instance read failed") from None
        instance_id = item.get("currentInstanceId")
        if instance_id is not None and not isinstance(instance_id, str):
            raise LifecycleRepositoryError("Cosmos trigger-instance record is malformed")
        return instance_id

    def save_trigger_instance_id(self, source_id: str, control_id: str, instance_id: str) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        item = {
            "id": control_id,
            "sourceId": source_id,
            "currentInstanceId": instance_id,
            "updatedAt": _now_iso(),
        }
        try:
            self._ingestion_runs.upsert_item(body=item)
        except Exception:
            raise LifecycleRepositoryError("Cosmos trigger-instance save failed") from None

    def get_webhook_subscription_id(self, source_id: str) -> str | None:
        try:
            item = self._ingestion_runs.read_item(item=WEBHOOK_CONTROL_ID, partition_key=source_id)
        except Exception as error:
            if _error_status(error) == 404:
                return None
            raise LifecycleRepositoryError("Cosmos webhook subscription read failed") from None
        sub_id = item.get("subscriptionId")
        if sub_id is not None and not isinstance(sub_id, str):
            raise LifecycleRepositoryError("Cosmos webhook subscription ID is malformed")
        return sub_id

    def save_webhook_subscription_id(self, source_id: str, subscription_id: str) -> None:
        if not subscription_id:
            raise ValueError("subscription_id is required")
        item = {
            "id": WEBHOOK_CONTROL_ID,
            "sourceId": source_id,
            "subscriptionId": subscription_id,
            "updatedAt": _now_iso(),
        }
        try:
            self._ingestion_runs.upsert_item(body=item)
        except Exception:
            raise LifecycleRepositoryError("Cosmos webhook subscription save failed") from None


def _ready_ref_from_row(row: Mapping[str, Any]) -> ReadyDocumentRef:
    try:
        return ReadyDocumentRef(
            document_id=row["documentId"],
            source_run_id=row["sourceRunId"],
            document_key=row["documentKey"],
            item_id=row["itemId"],
            allowed_group_ids=tuple(row["allowedGroupIds"]),
            acl_hash=row["aclHash"],
            etag=row["_etag"],
        )
    except KeyError as error:
        raise LifecycleRepositoryError("ready-document row is missing an expected field") from error


def _error_status(error: Exception) -> int | None:
    for attribute in ("status_code", "status"):
        status = getattr(error, attribute, None)
        if isinstance(status, int):
            return status
    return None


def _validate_page_size(page_size: int) -> None:
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= INTERNAL_PAGE_SIZE
    ):
        raise ValueError(f"page_size must be between 1 and {INTERNAL_PAGE_SIZE}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
