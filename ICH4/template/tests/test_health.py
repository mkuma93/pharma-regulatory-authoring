"""Smoke test — GET /health must return 200 {"status": "ok"}."""
import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GCP_PROJECT_ID", "")

from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
