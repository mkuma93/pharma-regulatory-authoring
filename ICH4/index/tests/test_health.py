"""Basic smoke tests for the ICH4 index service."""
from fastapi.testclient import TestClient


def test_health():
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    os.environ.setdefault("LLAMA_CLOUD_API_KEY", "test-key")
    os.environ.setdefault("GCP_PROJECT_ID", "")
    os.environ.setdefault("GCS_BUCKET_NAME", "")

    from api.app import app
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
