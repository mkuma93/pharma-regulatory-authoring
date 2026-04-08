"""
Tests for api/app.py

Covers:
  - GET /health → {"status": "ok"}
  - POST /health → 405
  - IAPMiddleware is registered on the app
  - /index/query route is registered
  - App starts without error when GCP / GCS env vars are empty
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import app


class TestHealthEndpoint:
    def test_returns_status_ok(self):
        with TestClient(app) as client:
            assert client.get("/health").json() == {"status": "ok"}

    def test_health_returns_200(self):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    def test_health_not_available_via_post(self):
        with TestClient(app) as client:
            assert client.post("/health").status_code == 405


class TestAppStructure:
    def test_iap_middleware_registered(self):
        from api.middleware.iap import IAPMiddleware
        middleware_classes = [m.cls for m in app.user_middleware]
        assert IAPMiddleware in middleware_classes

    def test_index_query_route_registered(self):
        routes = {r.path for r in app.routes}  # type: ignore[attr-defined]
        assert "/index/query" in routes

    def test_app_title_mentions_ich_or_ctd(self):
        assert "ICH" in app.title or "CTD" in app.title


class TestAppStartup:
    def test_starts_without_error_when_no_gcp(self, monkeypatch):
        """Lifespan completes cleanly when GCP project and GCS bucket are unset."""
        monkeypatch.setenv("GCP_PROJECT_ID", "")
        monkeypatch.setenv("GCS_BUCKET_NAME", "")
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
