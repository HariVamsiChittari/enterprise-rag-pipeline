"""Centralized configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(f"Required environment variable {name} is not set")
    return value


def _bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("true", "1", "yes")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class IngestionConfig:
    # Feature toggles
    extraction_enabled: bool
    enrichment_enabled: bool
    summary_enabled: bool
    key_phrases_enabled: bool
    entities_enabled: bool
    allowed_extensions: tuple[str, ...]

    # SharePoint / Graph
    source_id: str
    drive_id: str
    tenant_id: str
    app_client_id: str
    certificate_secret_name: str
    key_vault_uri: str

    # Azure services
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_ingestion_runs_container: str
    cosmos_source_documents_container: str
    cosmos_search_chunks_container: str
    document_intelligence_endpoint: str
    language_endpoint: str
    openai_endpoint: str
    managed_identity_client_id: str


def load_config() -> IngestionConfig:
    extensions_raw = os.getenv("ALLOWED_FILE_EXTENSIONS", ".pdf")
    extensions = tuple(ext.strip().lower() for ext in extensions_raw.split(",") if ext.strip())

    summary = _bool("SUMMARY_ENABLED", False)
    key_phrases = _bool("KEY_PHRASES_ENABLED", True)
    entities = _bool("ENTITIES_ENABLED", True)
    any_enrichment = summary or key_phrases or entities

    return IngestionConfig(
        extraction_enabled=_bool("EXTRACTION_ENABLED", True),
        enrichment_enabled=any_enrichment,
        summary_enabled=summary and any_enrichment,
        key_phrases_enabled=key_phrases and any_enrichment,
        entities_enabled=entities and any_enrichment,
        allowed_extensions=extensions,
        source_id=_required("INGESTION_SOURCE_ID"),
        drive_id=_required("SHAREPOINT_ASSIGNED_DRIVE_ID"),
        tenant_id=_required("SHAREPOINT_TENANT_ID"),
        app_client_id=_required("SHAREPOINT_APP_CLIENT_ID"),
        certificate_secret_name=os.getenv("SHAREPOINT_CERTIFICATE_SECRET_NAME", "sharepoint-app-cert"),
        key_vault_uri=_required("KEY_VAULT_URI"),
        cosmos_endpoint=_required("COSMOS_ENDPOINT"),
        cosmos_database=_required("COSMOS_DATABASE_NAME"),
        cosmos_ingestion_runs_container=os.getenv("COSMOS_INGESTION_RUNS_CONTAINER_NAME", "ingestion-runs"),
        cosmos_source_documents_container=os.getenv("COSMOS_SOURCE_DOCUMENTS_CONTAINER_NAME", "source-documents"),
        cosmos_search_chunks_container=os.getenv("COSMOS_SEARCH_CHUNKS_CONTAINER_NAME", "search-chunks"),
        document_intelligence_endpoint=_required("DOCUMENT_INTELLIGENCE_ENDPOINT") if _bool("EXTRACTION_ENABLED", True) else "",
        language_endpoint=_required("AZURE_LANGUAGE_ENDPOINT") if any_enrichment else "",
        openai_endpoint=_required("OPENAI_ENDPOINT"),
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID", ""),
    )
