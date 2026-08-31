from __future__ import annotations

import pytest

from config import load_config


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "EXTRACTION_ENABLED": "false",
        "SUMMARY_ENABLED": "false",
        "KEY_PHRASES_ENABLED": "false",
        "ENTITIES_ENABLED": "false",
        "INGESTION_SOURCE_ID": "source",
        "SHAREPOINT_ASSIGNED_DRIVE_ID": "drive",
        "SHAREPOINT_TENANT_ID": "tenant",
        "SHAREPOINT_APP_CLIENT_ID": "client",
        "KEY_VAULT_URI": "https://vault.example",
        "COSMOS_ENDPOINT": "https://cosmos.example",
        "COSMOS_DATABASE_NAME": "database",
        "OPENAI_ENDPOINT": "https://openai.example",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_load_config_requires_sharepoint_site_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.delenv("SHAREPOINT_SITE_URL", raising=False)

    with pytest.raises(EnvironmentError, match="SHAREPOINT_SITE_URL"):
        load_config()


def test_load_config_reads_required_sharepoint_site_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("SHAREPOINT_SITE_URL", " https://tenant.sharepoint.com/sites/site ")

    config = load_config()

    assert config.sharepoint_site_url == "https://tenant.sharepoint.com/sites/site"