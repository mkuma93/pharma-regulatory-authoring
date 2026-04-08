"""Smoke test: GET /health returns 200 + {"status": "ok"}."""
import os
import sys

import pytest
from fastapi.testclient import TestClient

# Ensure OPENAI_API_KEY is set so Settings() doesn't raise on import
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GCP_PROJECT_ID", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api.app import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def test_health_returns_200():
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body():
    resp = client.get("/health")
    assert resp.json() == {"status": "ok"}
