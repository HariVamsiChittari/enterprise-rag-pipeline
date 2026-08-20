"""Versioned contracts for full-sync ingestion and Cosmos persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


SCHEMA_VERSION = 1
SOURCE_CONTROL_ID = "source-control"
MAX_DOCUMENT_ITEM_BYTES = 128 * 1024
MAX_CHUNK_ITEM_BYTES = 256 * 1024
ENRICHMENT_MODULES = ("summary", "key_phrases", "entities")


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    TERMINATED = "terminated"


class RunStage(str, Enum):
    ACTIVATING = "activating"
    CLEANING = "cleaning"
    DISCOVERING = "discovering"
    PROCESSING = "processing"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


class DocumentStatus(str, Enum):
    DISCOVERED = "discovered"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    RETIRED = "retired"


# Reasons a previously-ready document can leave retrieval: permission loss detected by
# ACL resync, a newer version superseding it, or the source item being deleted.
RETIRED_REASONS = frozenset({"acl_revoked", "superseded", "deleted"})


class DocumentStage(str, Enum):
    DISCOVERED = "discovered"
    ACL = "acl"
    DOWNLOAD = "download"
    EXTRACTION = "extraction"
    CHUNKING = "chunking"
    ENRICHMENT = "enrichment"
    EMBEDDING = "embedding"
    PERSISTING = "persisting"
    VERIFYING = "verifying"
    TERMINAL = "terminal"


class ModuleStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ActivityStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SafeError:
    code: str
    stage: str
    retryable: bool

    def __post_init__(self) -> None:
        _require_text("error code", self.code, 100)
        _require_text("error stage", self.stage, 100)


@dataclass(frozen=True)
class ScaleLimits:
    max_eligible_pdfs: int = 10_000
    max_drive_items: int = 50_000
    max_folders: int = 10_000
    max_folder_depth: int = 32
    max_graph_pages: int = 20_000
    max_pdf_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 500
    max_chunks_per_pdf: int = 2_000

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) <= 0:
                raise ValueError(f"{field.name} must be positive")


@dataclass(frozen=True)
class ExtractionProfile:
    version: str = "document-intelligence-layout-v1"
    model: str = "prebuilt-layout"
    output_format: str = "markdown"

    def __post_init__(self) -> None:
        _validate_profile_texts(self)


@dataclass(frozen=True)
class ChunkingProfile:
    version: str = "layout-page-token-v1"
    strategy: str = "layout-page-token"
    tokenizer: str = "cl100k_base"
    max_tokens: int = 800
    overlap_tokens: int = 100

    def __post_init__(self) -> None:
        _validate_profile_texts(self)
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.overlap_tokens < 0 or self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be between zero and max_tokens")


@dataclass(frozen=True)
class EnrichmentProfile:
    version: str = "language-all-v1"
    summary_enabled: bool = False
    key_phrases_enabled: bool = True
    entities_enabled: bool = True
    max_key_phrases: int = 25
    max_entities: int = 50
    enabled_modules: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        _require_text("enrichment profile version", self.version, 100)
        if self.max_key_phrases <= 0 or self.max_entities <= 0:
            raise ValueError("enrichment output limits must be positive")
        enabled: list[str] = []
        if self.summary_enabled:
            enabled.append("summary")
        if self.key_phrases_enabled:
            enabled.append("key_phrases")
        if self.entities_enabled:
            enabled.append("entities")
        object.__setattr__(self, "enabled_modules", tuple(enabled))


@dataclass(frozen=True)
class EmbeddingProfile:
    version: str = "text-embedding-3-large-3072-v1"
    model: str = "text-embedding-3-large"
    deployment: str = "text-embedding-3-large"
    dimensions: int = 3_072
    distance_function: str = "cosine"

    def __post_init__(self) -> None:
        _validate_profile_texts(self)
        if self.dimensions != 3_072:
            raise ValueError("schema version 1 requires 3072 embedding dimensions")
        if self.distance_function != "cosine":
            raise ValueError("only cosine distance is supported by schema version 1")


@dataclass(frozen=True)
class ProfileSnapshot:
    extraction: ExtractionProfile = field(default_factory=ExtractionProfile)
    chunking: ChunkingProfile = field(default_factory=ChunkingProfile)
    enrichment: EnrichmentProfile = field(default_factory=EnrichmentProfile)
    embedding: EmbeddingProfile = field(default_factory=EmbeddingProfile)


@dataclass(frozen=True)
class Settings:
    source_id: str
    drive_id: str
    code_version: str
    limits: ScaleLimits = field(default_factory=ScaleLimits)
    profiles: ProfileSnapshot = field(default_factory=ProfileSnapshot)
    activity_timeout_seconds: float = 300.0
    document_wave_size: int = 4
    activity_attempts: int = 5

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id, 200)
        _require_text("drive_id", self.drive_id, 500)
        _require_text("code_version", self.code_version, 100)
        if self.activity_timeout_seconds <= 0:
            raise ValueError("activity_timeout_seconds must be positive")



@dataclass(frozen=True)
class RunCounters:
    discovered: int = 0
    processing: int = 0
    ready: int = 0
    failed: int = 0
    chunks_written: int = 0
    retries: int = 0
    items_scanned: int = 0

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) < 0:
                raise ValueError(f"{field.name} cannot be negative")


@dataclass(frozen=True)
class SourceControlRecord:
    source_id: str
    current_run_id: str
    current_orchestration_instance_id: str
    activated_at: str
    updated_at: str
    last_completed_run_id: str | None = None
    id: str = SOURCE_CONTROL_ID
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.id != SOURCE_CONTROL_ID:
            raise ValueError("invalid source-control discriminator")
        _require_schema_version(self.schema_version)
        _require_text("source_id", self.source_id, 200)
        _require_text("current_run_id", self.current_run_id, 100)
        _require_text("current_orchestration_instance_id", self.current_orchestration_instance_id, 100)
        _require_utc("activated_at", self.activated_at)
        _require_utc("updated_at", self.updated_at)

    def to_cosmos_item(self) -> dict[str, Any]:
        return _serialize_dataclass(self)


@dataclass(frozen=True)
class IngestionRunRecord:
    source_id: str
    run_id: str
    drive_id: str
    orchestration_instance_id: str
    status: RunStatus
    stage: RunStage
    started_at: str
    activated_at: str
    updated_at: str
    counters: RunCounters
    profiles: ProfileSnapshot
    ingestion_mode: str
    completed_at: str | None = None
    error: SafeError | None = None
    id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        expected_id = run_record_id(self.run_id)
        if self.id != expected_id:
            raise ValueError(f"run id must be {expected_id}")
        _require_schema_version(self.schema_version)
        for name, value, maximum in (
            ("source_id", self.source_id, 200),
            ("run_id", self.run_id, 100),
            ("drive_id", self.drive_id, 500),
            ("orchestration_instance_id", self.orchestration_instance_id, 100),
            ("ingestion_mode", self.ingestion_mode, 100),
        ):
            _require_text(name, value, maximum)
        for name, value in (
            ("started_at", self.started_at),
            ("activated_at", self.activated_at),
            ("updated_at", self.updated_at),
        ):
            _require_utc(name, value)
        if self.completed_at is not None:
            _require_utc("completed_at", self.completed_at)

    def to_cosmos_item(self) -> dict[str, Any]:
        return _serialize_dataclass(self)


@dataclass(frozen=True)
class SourceDocumentRecord:
    source_id: str
    run_id: str
    drive_id: str
    item_id: str
    parent_item_id: str
    source_name: str
    source_path: str
    source_url: str
    e_tag: str
    mime_type: str
    size_bytes: int
    discovery_ordinal: int
    allowed_group_ids: tuple[str, ...]
    acl_hash: str
    acl_evaluated_at: str
    status: DocumentStatus
    stage: DocumentStage
    attempt_count: int
    discovered_at: str
    updated_at: str
    page_count: int | None = None
    expected_chunk_count: int | None = None
    written_chunk_count: int | None = None
    content_hash: str | None = None
    extraction_mode: str | None = None
    processing_started_at: str | None = None
    ready_at: str | None = None
    failed_at: str | None = None
    error: SafeError | None = None
    retired_at: str | None = None
    retired_reason: str | None = None
    retried_at: str | None = None
    ingestion_mode: str = "full-sync"
    id: str = ""
    document_id: str = ""
    source_run_id: str = ""
    document_key: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        expected_document_id = create_document_id(self.source_id, self.drive_id, self.item_id)
        expected_source_run_id = create_source_run_id(self.source_id, self.run_id)
        expected_document_key = create_document_key(self.source_id, self.run_id, expected_document_id)
        if self.id != expected_document_id or self.document_id != expected_document_id:
            raise ValueError("document identifiers do not match their deterministic value")
        if self.source_run_id != expected_source_run_id or self.document_key != expected_document_key:
            raise ValueError("document run keys do not match their deterministic value")
        _require_schema_version(self.schema_version)
        _validate_document_fields(self)
        _validate_sorted_unique("allowed_group_ids", self.allowed_group_ids, require_nonempty=True)
        _require_sha256("acl_hash", self.acl_hash)
        if self.content_hash is not None:
            _require_sha256("content_hash", self.content_hash)
        _validate_optional_utc_fields(self)
        _validate_retirement(self)
        if serialized_size_bytes(self.to_cosmos_item()) > MAX_DOCUMENT_ITEM_BYTES:
            raise ValueError("document item exceeds 128 KiB")

    def to_cosmos_item(self) -> dict[str, Any]:
        return _serialize_dataclass(self)


@dataclass(frozen=True)
class EnrichmentStatuses:
    summary: ModuleStatus
    key_phrases: ModuleStatus
    entities: ModuleStatus


@dataclass(frozen=True)
class Entity:
    text: str
    category: str
    subcategory: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        _require_text("entity text", self.text, 500)
        _require_text("entity category", self.category, 100)
        if self.subcategory is not None:
            _require_text("entity subcategory", self.subcategory, 100)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("entity confidence must be between zero and one")


@dataclass(frozen=True)
class SearchChunkRecord:
    source_id: str
    run_id: str
    document_id: str
    document_key: str
    allowed_group_ids: tuple[str, ...]
    source_name: str
    source_url: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    chunk_index: int
    created_at: str
    content: str
    content_hash: str
    embedding_text: str
    searchable_text: str
    token_count: int
    enrichment_status: EnrichmentStatuses
    summary: str | None
    key_phrases: tuple[str, ...]
    entities: tuple[Entity, ...]
    language_code: str
    embedding: tuple[float, ...]
    embedded_at: str
    id: str = ""
    source_run_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        expected_id = create_chunk_id(self.chunk_index)
        expected_source_run_id = create_source_run_id(self.source_id, self.run_id)
        expected_document_key = create_document_key(self.source_id, self.run_id, self.document_id)
        if self.id != expected_id or self.source_run_id != expected_source_run_id:
            raise ValueError("chunk identifiers do not match their deterministic value")
        if self.document_key != expected_document_key:
            raise ValueError("chunk document key does not match its deterministic value")
        _require_schema_version(self.schema_version)
        _validate_chunk_fields(self)
        if serialized_size_bytes(self.to_cosmos_item()) > MAX_CHUNK_ITEM_BYTES:
            raise ValueError("chunk item exceeds 256 KiB")

    def to_cosmos_item(self) -> dict[str, Any]:
        return _serialize_dataclass(self)


@dataclass(frozen=True)
class ActivityOutcome:
    document_id: str
    status: ActivityStatus
    chunks_written: int
    retry_count: int
    error: SafeError | None = None

    def __post_init__(self) -> None:
        _require_text("document_id", self.document_id, 64)
        if self.chunks_written < 0 or self.retry_count < 0:
            raise ValueError("activity counters cannot be negative")
        if self.status is ActivityStatus.FAILED and self.error is None:
            raise ValueError("failed activities require a safe error")
        if self.status is not ActivityStatus.FAILED and self.error is not None:
            raise ValueError("only failed activities can contain an error")

    def to_dict(self) -> dict[str, Any]:
        return _serialize_dataclass(self)


def create_run_id(timestamp: datetime, entropy: str) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    _require_text("run entropy", entropy, 500)
    timestamp_part = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    entropy_part = hashlib.sha256(entropy.encode("utf-8")).hexdigest()[:16]
    return f"{timestamp_part}-{entropy_part}"


def run_record_id(run_id: str) -> str:
    _require_text("run_id", run_id, 100)
    return f"run:{run_id}"


def create_orchestration_instance_id(source_id: str) -> str:
    _require_text("source_id", source_id, 200)
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:32]
    return f"full-sync-{digest}"


def create_source_run_id(source_id: str, run_id: str) -> str:
    _require_text("source_id", source_id, 200)
    _require_text("run_id", run_id, 100)
    return f"{source_id}:{run_id}"


def create_document_id(source_id: str, drive_id: str, item_id: str) -> str:
    for name, value, maximum in (
        ("source_id", source_id, 200),
        ("drive_id", drive_id, 500),
        ("item_id", item_id, 500),
    ):
        _require_text(name, value, maximum)
    identity = json.dumps([source_id, drive_id, item_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def create_document_key(source_id: str, run_id: str, document_id: str) -> str:
    _require_sha256("document_id", document_id)
    return f"{create_source_run_id(source_id, run_id)}:{document_id}"


def create_chunk_id(chunk_index: int) -> str:
    if chunk_index < 0 or chunk_index > 999_999:
        raise ValueError("chunk_index must be between 0 and 999999")
    return f"chunk:{chunk_index:06d}"


def canonical_group_ids(group_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(sorted({_require_text("group id", group_id, 100) for group_id in group_ids}))
    if not canonical:
        raise ValueError("ACL must contain at least one verified group")
    return canonical


def content_sha256(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_error_from_exception(error: BaseException, stage: str) -> SafeError:
    if isinstance(error, TimeoutError):
        return SafeError("dependency_timeout", stage, True)
    if isinstance(error, PermissionError):
        return SafeError("authorization_failed", stage, False)
    if isinstance(error, ValueError):
        return SafeError("invalid_data", stage, False)
    return SafeError("internal_error", stage, False)


def serialized_size_bytes(item: Mapping[str, Any]) -> int:
    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _serialize_dataclass(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            _snake_to_camel(field.name): _serialize_dataclass(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _serialize_dataclass(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize_dataclass(item) for item in value]
    return value


def _snake_to_camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


def _require_text(name: str, value: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value.strip()


def _require_sha256(name: str, value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_utc(name: str, value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be a UTC timestamp")


def _require_schema_version(value: int) -> None:
    if value != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")


def _validate_profile_texts(profile: Any) -> None:
    for field in fields(profile):
        value = getattr(profile, field.name)
        if isinstance(value, str):
            _require_text(field.name, value, 200)


def _validate_sorted_unique(name: str, values: tuple[str, ...], require_nonempty: bool = False) -> None:
    if require_nonempty and not values:
        raise ValueError(f"{name} cannot be empty")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{name} must be sorted and unique")
    for value in values:
        _require_text(name, value, 500)


def _validate_document_fields(record: SourceDocumentRecord) -> None:
    for name, value, maximum in (
        ("source_id", record.source_id, 200),
        ("run_id", record.run_id, 100),
        ("drive_id", record.drive_id, 500),
        ("item_id", record.item_id, 500),
        ("source_name", record.source_name, 500),
        ("mime_type", record.mime_type, 200),
    ):
        _require_text(name, value, maximum)
    if record.mime_type != "application/pdf":
        raise ValueError("only PDF documents are supported")
    if record.size_bytes < 0 or record.discovery_ordinal < 0 or record.attempt_count < 0:
        raise ValueError("document counters cannot be negative")
    for count_name in ("page_count", "expected_chunk_count", "written_chunk_count"):
        count = getattr(record, count_name)
        if count is not None and count < 0:
            raise ValueError(f"{count_name} cannot be negative")


def _validate_optional_utc_fields(record: SourceDocumentRecord) -> None:
    _require_utc("acl_evaluated_at", record.acl_evaluated_at)
    _require_utc("discovered_at", record.discovered_at)
    _require_utc("updated_at", record.updated_at)
    for field_name in ("processing_started_at", "ready_at", "failed_at", "retired_at"):
        value = getattr(record, field_name)
        if value is not None:
            _require_utc(field_name, value)


def _validate_retirement(record: SourceDocumentRecord) -> None:
    is_retired = record.status is DocumentStatus.RETIRED
    has_retirement_fields = record.retired_at is not None and record.retired_reason is not None
    if is_retired != has_retirement_fields:
        raise ValueError("retired documents require retired_at and retired_reason; others must omit them")
    if is_retired and record.ready_at is None:
        raise ValueError("only a previously ready document can be retired")
    if is_retired and (record.failed_at is not None or record.error is not None):
        raise ValueError("retired documents cannot carry a failed_at or error")
    if record.retired_reason is not None and record.retired_reason not in RETIRED_REASONS:
        raise ValueError("retired_reason must be one of the supported values")


def _validate_chunk_fields(record: SearchChunkRecord) -> None:
    _validate_sorted_unique("allowed_group_ids", record.allowed_group_ids, require_nonempty=True)
    _validate_sorted_unique("key_phrases", record.key_phrases)
    if record.page_start < 1 or record.page_end < record.page_start:
        raise ValueError("chunk page range is invalid")
    if record.token_count <= 0:
        raise ValueError("chunk token count must be positive")
    if len(record.embedding) != 3072:
        raise ValueError("embedding length does not match expected 3072 dimensions")
    if record.language_code != "en":
        raise ValueError("schema version 1 supports only English")
    if len(record.key_phrases) > 25 or len(record.entities) > 50:
        raise ValueError("enrichment output exceeds schema bounds")
    for name, value, maximum in (
        ("source_id", record.source_id, 200),
        ("run_id", record.run_id, 100),
        ("document_id", record.document_id, 64),
        ("source_name", record.source_name, 500),
        ("content", record.content, 128_000),
        ("embedding_text", record.embedding_text, 128_000),
    ):
        _require_text(name, value, maximum)
    _require_sha256("content_hash", record.content_hash)
    _require_utc("created_at", record.created_at)
    _require_utc("embedded_at", record.embedded_at)


@dataclass(frozen=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    page_number: int
    content: str
