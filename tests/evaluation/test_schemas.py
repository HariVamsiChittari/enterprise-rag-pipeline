from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


PROJECT_ROOT = Path(__file__).parents[2]
SCHEMA_ROOT = PROJECT_ROOT / "evaluation" / "schemas"
SHA256 = "a" * 64


def load_validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_dataset_manifest_accepts_complete_fabricated_record() -> None:
    record = {
        "datasetVersion": "dataset-v1",
        "domain": "fabricated policy corpus",
        "useCase": "Validate grounded answers without protected content.",
        "source": {
            "sourceId": "fabricated-source",
            "driveId": "fabricated-drive",
            "libraryFingerprint": SHA256,
        },
        "classifications": ["internal"],
        "contentVariants": ["headings", "tables"],
        "documents": [
            {
                "itemId": "fabricated-item",
                "eTag": "fabricated-etag",
                "contentHash": SHA256,
                "classification": "internal",
                "format": "pdf",
                "structures": ["headings", "tables"],
                "securityClass": "group-restricted",
            }
        ],
        "coverage": {
            "documentCount": 1,
            "queryTargetCount": 100,
            "classificationCounts": {"internal": 1},
            "variantCounts": {"headings": 1, "tables": 1},
        },
        "createdAt": "2026-08-05T12:00:00Z",
    }

    load_validator("dataset-manifest.schema.json").validate(record)


@pytest.mark.parametrize(
    ("schema_name", "record"),
    [
        (
            "ground-truth-record.schema.json",
            {
                "queryId": "Q-001",
                "category": "answerable",
                "question": "What is the fabricated retention period?",
                "expectedContext": [
                    {
                        "documentItemId": "fabricated-item",
                        "pageStart": 1,
                        "pageEnd": 1,
                        "exactText": "Fabricated records are retained for thirty days.",
                    }
                ],
                "expectedAnswer": "Thirty days.",
                "expectedCitations": ["fabricated-item#page=1"],
                "principalCaseLabel": "authorized-reader",
                "smeNotes": "Fabricated unit-test record.",
            },
        ),
        (
            "principal-cases.schema.json",
            {
                "version": "principals-v1",
                "cases": [
                    {
                        "label": "authorized-reader",
                        "userObjectId": "fabricated-user",
                        "groupIds": ["fabricated-group"],
                        "expectedAllowDocumentIds": ["fabricated-item"],
                        "expectedDenyDocumentIds": ["fabricated-denied-item"],
                    }
                ],
            },
        ),
        (
            "approval.schema.json",
            {
                "datasetHash": SHA256,
                "approverIdentity": "fabricated-sme",
                "approvedAt": "2026-08-05T12:00:00Z",
                "status": "approved",
                "reviewNotes": "Fabricated approval.",
                "expiresAt": "2027-08-05T12:00:00Z",
            },
        ),
        (
            "experiment-manifest.schema.json",
            {
                "experimentId": "experiment-v1-run-1",
                "datasetHash": SHA256,
                "datasetComponents": {
                    "groundTruthHash": SHA256,
                    "principalCasesHash": SHA256,
                    "approvalHash": SHA256,
                    "datasetManifestHash": SHA256,
                },
                "sourceId": "fabricated-source",
                "runId": "fabricated-run",
                "codeCommit": "0" * 64,
                "sourceTreeHash": SHA256,
                "submittedContextHash": SHA256,
                "imageDigest": "sha256:" + SHA256,
                "baseImageDigest": "sha256:" + SHA256,
                "dependencyLockHash": SHA256,
                "acrBuildId": "fabricated-build-id",
                "catalogSha": SHA256,
                "profiles": {
                    profile_name: {"version": f"{profile_name}-v1", "parameters": {}}
                    for profile_name in (
                        "extraction",
                        "chunking",
                        "cleaning",
                        "enrichment",
                        "embedding",
                        "retrieval",
                        "prompt",
                    )
                },
                "modelDeployments": {
                    "embedding": "fabricated-embedding",
                    "chat": "fabricated-chat",
                    "evaluator": "fabricated-evaluator",
                },
                "environment": "evaluation",
                "evaluatorVersions": {"fabricated-evaluator": "1.0.0"},
                "pythonVersion": "3.12.7",
                "principalCase": "authorized-reader",
                "retrievalMode": "hybrid",
                "k": 5,
                "repeatIndex": 1,
                "seed": 0,
                "evaluationAsOf": "2026-08-05T12:00:00Z",
                "candidateSetHash": SHA256,
                "baselineRankingHash": SHA256,
                "candidateRankingHash": SHA256,
                "startedAt": "2026-08-05T12:00:00Z",
            },
        ),
        (
            "ranking-record.schema.json",
            {
                "queryId": "Q-001",
                "retrievedContext": [
                    {"documentItemId": "fabricated-item", "pageNumber": 1},
                    {"documentItemId": "fabricated-item", "pageNumber": 2},
                ],
            },
        ),
    ],
)
def test_protected_artifact_schemas_accept_fabricated_records(
    schema_name: str,
    record: dict[str, object],
) -> None:
    load_validator(schema_name).validate(record)


def _fabricated_manifest() -> dict[str, object]:
    return {
        "experimentId": "experiment-v1-run-1",
        "datasetHash": SHA256,
        "datasetComponents": {
            "groundTruthHash": SHA256,
            "principalCasesHash": SHA256,
            "approvalHash": SHA256,
            "datasetManifestHash": SHA256,
        },
        "sourceId": "fabricated-source",
        "runId": "fabricated-run",
        "codeCommit": "0" * 64,
        "sourceTreeHash": SHA256,
        "submittedContextHash": SHA256,
        "imageDigest": "sha256:" + SHA256,
        "baseImageDigest": "sha256:" + SHA256,
        "dependencyLockHash": SHA256,
        "acrBuildId": "fabricated-build-id",
        "catalogSha": SHA256,
        "profiles": {
            profile_name: {"version": f"{profile_name}-v1", "parameters": {}}
            for profile_name in (
                "extraction",
                "chunking",
                "cleaning",
                "enrichment",
                "embedding",
                "retrieval",
                "prompt",
            )
        },
        "modelDeployments": {
            "embedding": "fabricated-embedding",
            "chat": "fabricated-chat",
            "evaluator": "fabricated-evaluator",
        },
        "environment": "evaluation",
        "evaluatorVersions": {"fabricated-evaluator": "1.0.0"},
        "pythonVersion": "3.12.7",
        "principalCase": "authorized-reader",
        "retrievalMode": "hybrid",
        "k": 5,
        "repeatIndex": 1,
        "seed": 0,
        "evaluationAsOf": "2026-08-05T12:00:00Z",
        "candidateSetHash": SHA256,
        "baselineRankingHash": SHA256,
        "candidateRankingHash": SHA256,
        "startedAt": "2026-08-05T12:00:00Z",
    }


def test_experiment_manifest_rejects_short_commit() -> None:
    record = _fabricated_manifest()
    record["codeCommit"] = "abcdef0"
    with pytest.raises(ValidationError):
        load_validator("experiment-manifest.schema.json").validate(record)


def test_experiment_manifest_rejects_mutable_image_reference() -> None:
    record = _fabricated_manifest()
    record["imageDigest"] = "retrieval-agent:latest"
    with pytest.raises(ValidationError):
        load_validator("experiment-manifest.schema.json").validate(record)


def test_experiment_manifest_rejects_naive_evaluation_as_of() -> None:
    record = _fabricated_manifest()
    record["evaluationAsOf"] = "2026-08-05T12:00:00"
    with pytest.raises(ValidationError):
        load_validator("experiment-manifest.schema.json").validate(record)


def test_experiment_manifest_rejects_unknown_retrieval_mode() -> None:
    record = _fabricated_manifest()
    record["retrievalMode"] = "keyword"
    with pytest.raises(ValidationError):
        load_validator("experiment-manifest.schema.json").validate(record)


def test_experiment_manifest_rejects_missing_dataset_component_hash() -> None:
    record = _fabricated_manifest()
    del record["datasetComponents"]["principalCasesHash"]  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        load_validator("experiment-manifest.schema.json").validate(record)


def test_ranking_record_rejects_forbidden_fields() -> None:
    record = {
        "queryId": "Q-001",
        "retrievedContext": [
            {
                "documentItemId": "doc-a",
                "pageNumber": 1,
                "content": "PROTECTED",
            }
        ],
    }
    with pytest.raises(ValidationError):
        load_validator("ranking-record.schema.json").validate(record)


def test_ranking_record_rejects_zero_page_number() -> None:
    record = {
        "queryId": "Q-001",
        "retrievedContext": [{"documentItemId": "doc-a", "pageNumber": 0}],
    }
    with pytest.raises(ValidationError):
        load_validator("ranking-record.schema.json").validate(record)


def test_answerable_ground_truth_rejects_missing_context() -> None:
    record = {
        "queryId": "Q-002",
        "category": "answerable",
        "question": "A fabricated question?",
        "expectedContext": [],
        "expectedAnswer": "A fabricated answer.",
        "expectedCitations": [],
        "principalCaseLabel": "authorized-reader",
        "smeNotes": "",
    }

    with pytest.raises(ValidationError):
        load_validator("ground-truth-record.schema.json").validate(record)


def test_approval_rejects_non_sha256_dataset_hash() -> None:
    record = {
        "datasetHash": "not-a-hash",
        "approverIdentity": "fabricated-sme",
        "approvedAt": "2026-08-05T12:00:00Z",
        "status": "approved",
        "reviewNotes": "",
        "expiresAt": "2027-08-05T12:00:00Z",
    }

    with pytest.raises(ValidationError):
        load_validator("approval.schema.json").validate(record)