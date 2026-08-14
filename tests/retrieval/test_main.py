"""Integration tests for the FastAPI retrieval endpoints."""

from __future__ import annotations

import pytest

structlog = pytest.importorskip("structlog", reason="structlog not installed")

from fastapi.testclient import TestClient

from retrieval.main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health_live(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_query_missing_auth_returns_401(client):
    response = client.post("/api/query", json={"question": "What is RAG?"})
    assert response.status_code == 401
    assert "missing_auth_header" in response.json()["detail"]


def test_query_empty_question_returns_422(client):
    response = client.post(
        "/api/query",
        json={"question": ""},
        headers={"X-MS-CLIENT-PRINCIPAL": "dummy"},
    )
    assert response.status_code == 422


def test_query_question_too_long_returns_422(client):
    response = client.post(
        "/api/query",
        json={"question": "x" * 4001},
        headers={"X-MS-CLIENT-PRINCIPAL": "dummy"},
    )
    assert response.status_code == 422


def test_query_invalid_mode_returns_422(client):
    response = client.post(
        "/api/query",
        json={"question": "test", "mode": "invalid"},
        headers={"X-MS-CLIENT-PRINCIPAL": "dummy"},
    )
    assert response.status_code == 422
