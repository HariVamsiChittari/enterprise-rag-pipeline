"""Versioned retrieval catalogs stored in Azure Cosmos DB for NoSQL."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError
from jsonschema import Draft202012Validator, ValidationError

from retrieval.config_loader import (
    ConfigLoaderError,
    SCORING_PROFILES_SCHEMA,
    SYNONYM_MAPS_SCHEMA,
    load_scoring_profiles,
    load_synonym_maps,
    validate_profile_synonym_map_references,
)
from retrieval.scoring import ScoringProfile
from retrieval.synonyms import SynonymMap


CATALOG_SCHEMA_VERSION = 1
MAX_CATALOG_ITEM_BYTES = 1_572_864
CATALOG_TYPE = "retrieval-catalog"
ACTIVE_CATALOG_TYPE = "active-retrieval-catalog"
_ALLOWED_TEXT_WEIGHT_FIELDS = frozenset(
    {"content", "sourceName", "source_name", "sectionPath", "section_path", "keyPhrases", "key_phrases"}
)
_ALLOWED_FUNCTION_FIELDS = {
    "freshness": frozenset({"sourceModifiedAt", "source_modified_at"}),
}


CATALOG_ITEM_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "deploymentInstanceId", "type", "schemaVersion", "version", "createdAt", "config"],
    "properties": {
        "id": {"type": "string", "pattern": r"^catalog:[0-9a-f]{64}$"},
        "deploymentInstanceId": {"type": "string", "minLength": 1, "maxLength": 100},
        "type": {"const": CATALOG_TYPE},
        "schemaVersion": {"const": CATALOG_SCHEMA_VERSION},
        "version": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        "createdAt": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"},
        "config": {
            "type": "object",
            "additionalProperties": False,
            "required": ["retrieval", "defaultProfile", "synonymsEnabled", "profiles", "synonymMaps"],
            "properties": {
                "retrieval": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["overFetchFactor", "hybridWeights", "fullTextScoreScope"],
                    "properties": {
                        "overFetchFactor": {"type": "integer", "minimum": 1, "maximum": 50},
                        "hybridWeights": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["vector", "text"],
                            "properties": {
                                "vector": {"type": "number", "exclusiveMinimum": 0},
                                "text": {"type": "number", "exclusiveMinimum": 0},
                            },
                        },
                        "fullTextScoreScope": {"enum": ["Local", "Global"]},
                    },
                },
                "defaultProfile": {"type": "string", "minLength": 1},
                "synonymsEnabled": {"type": "boolean"},
                "profiles": SCORING_PROFILES_SCHEMA["properties"]["profiles"],
                "synonymMaps": SYNONYM_MAPS_SCHEMA["properties"]["maps"],
            },
        },
    },
}


ACTIVE_POINTER_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "deploymentInstanceId", "type", "catalogId", "version", "activatedAt", "activatedBy"],
    "properties": {
        "id": {"const": "active"},
        "deploymentInstanceId": {"type": "string", "minLength": 1, "maxLength": 100},
        "type": {"const": ACTIVE_CATALOG_TYPE},
        "catalogId": {"type": "string", "pattern": r"^catalog:[0-9a-f]{64}$"},
        "version": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        "activatedAt": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"},
        "activatedBy": {"type": "string", "minLength": 1, "maxLength": 200},
    },
}


class CatalogError(RuntimeError):
    """A catalog cannot be safely published, activated, or loaded."""


class CatalogConflictError(CatalogError):
    """The active pointer changed concurrently."""


@dataclass(frozen=True)
class RetrievalCatalog:
    deployment_instance_id: str
    catalog_id: str
    version: str
    created_at: str
    over_fetch_factor: int
    hybrid_weights: tuple[float, float]
    full_text_score_scope: str
    default_profile: str
    synonyms_enabled: bool
    profiles: dict[str, ScoringProfile]
    synonym_maps: dict[str, SynonymMap]


def build_catalog_item(
    source: Mapping[str, Any],
    deployment_instance_id: str,
    *,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate authoring JSON and add deterministic immutable Cosmos metadata."""
    if not isinstance(deployment_instance_id, str) or not deployment_instance_id.strip():
        raise CatalogError("deployment instance id is required")
    if set(source) != {"schemaVersion", "config"}:
        raise CatalogError("catalog source contains unknown top-level fields")
    if source.get("schemaVersion") != CATALOG_SCHEMA_VERSION or not isinstance(source.get("config"), dict):
        raise CatalogError("catalog source must contain schemaVersion=1 and config")
    _reject_non_finite(source)
    config = _normalize_json_numbers(source["config"])
    canonical_body = _canonical_json(
        {"schemaVersion": CATALOG_SCHEMA_VERSION, "config": config}
    )
    digest = hashlib.sha256(canonical_body).hexdigest()
    timestamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    item = {
        "id": f"catalog:{digest}",
        "deploymentInstanceId": deployment_instance_id.strip(),
        "type": CATALOG_TYPE,
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "version": f"sha256:{digest}",
        "createdAt": timestamp.isoformat().replace("+00:00", "Z"),
        "config": config,
    }
    load_catalog_item(item)
    encoded = _canonical_json(item)
    if len(encoded) > MAX_CATALOG_ITEM_BYTES:
        raise CatalogError(
            f"catalog item exceeds {MAX_CATALOG_ITEM_BYTES} UTF-8 bytes"
        )
    return item


def load_catalog_item(item: Mapping[str, Any]) -> RetrievalCatalog:
    """Validate one immutable Cosmos item and construct runtime objects."""
    values = _without_cosmos_system_properties(item)
    if len(_canonical_json(values)) > MAX_CATALOG_ITEM_BYTES:
        raise CatalogError(
            f"catalog item exceeds {MAX_CATALOG_ITEM_BYTES} UTF-8 bytes"
        )
    _validate_schema(CATALOG_ITEM_SCHEMA, values, "catalog")
    _reject_non_finite(values)
    config = values["config"]
    _validate_supported_fields(config)
    expected_digest = hashlib.sha256(
        _canonical_json({"schemaVersion": values["schemaVersion"], "config": config})
    ).hexdigest()
    if values["id"] != f"catalog:{expected_digest}" or values["version"] != f"sha256:{expected_digest}":
        raise CatalogError("catalog id or version does not match its content")

    try:
        profiles = load_scoring_profiles({"profiles": config["profiles"]})
        synonym_maps = load_synonym_maps({"maps": config["synonymMaps"]})
        validate_profile_synonym_map_references(profiles, synonym_maps)
    except ConfigLoaderError as error:
        raise CatalogError(str(error)) from None

    default_profile = config["defaultProfile"]
    if default_profile not in profiles:
        raise CatalogError(f"default profile '{default_profile}' is not defined")
    retrieval = config["retrieval"]
    weights = retrieval["hybridWeights"]
    return RetrievalCatalog(
        deployment_instance_id=values["deploymentInstanceId"],
        catalog_id=values["id"],
        version=values["version"],
        created_at=values["createdAt"],
        over_fetch_factor=retrieval["overFetchFactor"],
        hybrid_weights=(float(weights["vector"]), float(weights["text"])),
        full_text_score_scope=retrieval["fullTextScoreScope"],
        default_profile=default_profile,
        synonyms_enabled=config["synonymsEnabled"],
        profiles=profiles,
        synonym_maps=synonym_maps,
    )


class CosmosCatalogLoader:
    def __init__(
        self,
        container: Any,
        deployment_instance_id: str,
        catalog_version: str,
    ) -> None:
        self._container = container
        self._deployment_instance_id = deployment_instance_id
        if (
            not isinstance(catalog_version, str)
            or len(catalog_version) != 71
            or not catalog_version.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in catalog_version[7:])
        ):
            raise CatalogError("catalog version must be sha256:<64 lowercase hex>")
        self._catalog_version = catalog_version

    def load(self) -> RetrievalCatalog:
        catalog_id = f"catalog:{self._catalog_version[7:]}"
        try:
            item = self._container.read_item(
                item=catalog_id,
                partition_key=self._deployment_instance_id,
            )
        except CosmosResourceNotFoundError:
            raise CatalogError("pinned retrieval catalog item is missing") from None
        except Exception:
            raise CatalogError("pinned retrieval catalog item read failed") from None
        catalog = load_catalog_item(item)
        if (
            catalog.deployment_instance_id != self._deployment_instance_id
            or catalog.version != self._catalog_version
            or catalog.catalog_id != catalog_id
        ):
            raise CatalogError("pinned retrieval catalog is inconsistent")
        return catalog


def publish_catalog(container: Any, item: dict[str, Any]) -> None:
    """Create one immutable catalog idempotently and verify the persisted content."""
    expected = load_catalog_item(item)
    try:
        container.create_item(body=item)
    except CosmosResourceExistsError:
        pass
    except Exception:
        raise CatalogError("catalog create failed") from None
    try:
        persisted = container.read_item(
            item=expected.catalog_id,
            partition_key=expected.deployment_instance_id,
        )
    except Exception:
        raise CatalogError("catalog verification read failed") from None
    verified = load_catalog_item(persisted)
    if verified.version != expected.version:
        raise CatalogError("persisted catalog verification failed")


def activate_catalog(
    container: Any,
    catalog: RetrievalCatalog,
    *,
    activated_by: str,
    expected_etag: str | None,
    activated_at: datetime | None = None,
) -> dict[str, Any]:
    """Create or ETag-replace the active pointer for a validated catalog."""
    if not activated_by.strip():
        raise CatalogError("activated_by is required")
    timestamp = (activated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    pointer = {
        "id": "active",
        "deploymentInstanceId": catalog.deployment_instance_id,
        "type": ACTIVE_CATALOG_TYPE,
        "catalogId": catalog.catalog_id,
        "version": catalog.version,
        "activatedAt": timestamp.isoformat().replace("+00:00", "Z"),
        "activatedBy": activated_by.strip(),
    }
    _validate_schema(ACTIVE_POINTER_SCHEMA, pointer, "active catalog pointer")
    try:
        if expected_etag is None:
            return container.create_item(body=pointer)
        return container.replace_item(
            item="active",
            body=pointer,
            etag=expected_etag,
            match_condition=MatchConditions.IfNotModified,
        )
    except CosmosResourceExistsError:
        raise CatalogConflictError("active catalog already exists") from None
    except Exception as error:
        if getattr(error, "status_code", None) == 412:
            raise CatalogConflictError("active catalog changed concurrently") from None
        raise CatalogError("active catalog update failed") from None


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CatalogError("catalog is not canonical JSON") from error


def _normalize_json_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {key: _normalize_json_numbers(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_json_numbers(child) for child in value]
    return value


def _validate_schema(schema: Mapping[str, Any], value: Mapping[str, Any], label: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        raise CatalogError(f"{label} is invalid: {error.message}") from None


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise CatalogError("catalog numbers must be finite")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def _validate_supported_fields(config: Mapping[str, Any]) -> None:
    for profile in config["profiles"]:
        unknown_weights = set(profile.get("textWeights", {})) - _ALLOWED_TEXT_WEIGHT_FIELDS
        if unknown_weights:
            raise CatalogError(f"unsupported text weight field: {sorted(unknown_weights)[0]}")
        for function in profile.get("functions", []):
            allowed_fields = _ALLOWED_FUNCTION_FIELDS.get(function["type"], frozenset())
            if function["fieldName"] not in allowed_fields:
                raise CatalogError(
                    f"unsupported {function['type']} field: {function['fieldName']}"
                )


def _without_cosmos_system_properties(item: Mapping[str, Any]) -> dict[str, Any]:
    system_properties = {"_rid", "_self", "_etag", "_attachments", "_ts"}
    return {key: value for key, value in item.items() if key not in system_properties}
