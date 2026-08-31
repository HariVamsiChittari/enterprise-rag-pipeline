from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = PROJECT_ROOT / "tools" / "publish_retrieval_catalog.py"


def _source() -> dict:
    return {
        "schemaVersion": 1,
        "config": {
            "retrieval": {
                "overFetchFactor": 3,
                "hybridWeights": {"vector": 2.0, "text": 1.0},
                "fullTextScoreScope": "Global",
            },
            "defaultProfile": "default",
            "synonymsEnabled": False,
            "profiles": [{
                "name": "default",
                "textWeights": {"content": 1.0},
                "functionAggregation": "sum",
                "functions": [],
            }],
            "synonymMaps": [],
        },
    }


def test_validate_prints_deterministic_catalog_identity(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_source()), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PUBLISHER), "validate", "--file", str(path), "--deployment-instance-id", "instance-a"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["catalogId"].startswith("catalog:")
    assert output["catalogDigest"].startswith("sha256:")


def test_validate_rejects_malformed_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PUBLISHER), "validate", "--file", str(path), "--deployment-instance-id", "instance-a"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "catalog source" in result.stderr


# --- Stale-intent guards (unit-level, no Cosmos) --------------------------------


import argparse

from tools.publish_retrieval_catalog import (
    StalePointerError,
    _verify_pointer_identity,
)


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "expected_pointer_id": "active",
        "expected_pointer_version": None,
        "expected_pointer_etag": None,
        "expect_no_pointer": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _pointer(**overrides) -> dict:
    base = {
        "id": "active",
        "version": "sha256:aaaa",
        "_etag": "etag-1",
    }
    base.update(overrides)
    return base


def test_verify_rejects_mutation_when_no_reviewer_state_supplied() -> None:
    import pytest

    with pytest.raises(StalePointerError, match="required"):
        _verify_pointer_identity(_pointer(), _args())


def test_verify_rejects_stale_version() -> None:
    import pytest

    args = _args(
        expected_pointer_version="sha256:bbbb",
        expected_pointer_etag="etag-1",
    )
    with pytest.raises(StalePointerError, match="version"):
        _verify_pointer_identity(_pointer(), args)


def test_verify_rejects_stale_etag() -> None:
    import pytest

    args = _args(
        expected_pointer_version="sha256:aaaa",
        expected_pointer_etag="etag-stale",
    )
    with pytest.raises(StalePointerError, match="ETag"):
        _verify_pointer_identity(_pointer(), args)


def test_verify_accepts_matching_reviewer_state_and_returns_etag() -> None:
    args = _args(
        expected_pointer_version="sha256:aaaa",
        expected_pointer_etag="etag-1",
    )
    assert _verify_pointer_identity(_pointer(), args) == "etag-1"


def test_verify_rejects_missing_pointer_without_expect_no_pointer() -> None:
    import pytest

    with pytest.raises(StalePointerError, match="expect-no-pointer"):
        _verify_pointer_identity(None, _args())


def test_verify_rejects_existing_pointer_with_expect_no_pointer() -> None:
    import pytest

    with pytest.raises(StalePointerError, match="expected no active pointer"):
        _verify_pointer_identity(_pointer(), _args(expect_no_pointer=True))


def test_verify_accepts_missing_pointer_with_expect_no_pointer() -> None:
    assert _verify_pointer_identity(None, _args(expect_no_pointer=True)) is None