"""Tests for api/middleware/iap.py — IAPMiddleware."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware.iap import IAP_HEADER, IAPMiddleware


def _make_app(audience: str = "") -> FastAPI:
    """Minimal FastAPI app with IAPMiddleware attached."""
    app = FastAPI()
    app.add_middleware(IAPMiddleware, audience=audience)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/protected")
    def protected():
        return {"data": "secret"}

    return app


class TestIAPMiddleware:
    # ── /health bypass ────────────────────────────────────────────────────────

    def test_health_always_allowed_without_audience(self):
        client = TestClient(_make_app(audience=""), raise_server_exceptions=True)
        assert client.get("/health").status_code == 200

    def test_health_always_allowed_with_audience_set(self):
        client = TestClient(_make_app(audience="/projects/123/global/backendServices/456"))
        assert client.get("/health").status_code == 200

    # ── IAP disabled — no audience ────────────────────────────────────────────

    def test_passes_through_when_no_audience(self):
        client = TestClient(_make_app(audience=""))
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json() == {"data": "secret"}

    # ── IAP enabled — missing JWT ─────────────────────────────────────────────

    def test_returns_401_when_jwt_header_missing(self):
        client = TestClient(_make_app(audience="/projects/123/global/backendServices/456"))
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert "Missing IAP" in resp.json()["detail"]

    # ── IAP enabled — invalid JWT ─────────────────────────────────────────────

    def test_returns_403_when_jwt_is_invalid(self):
        from google.oauth2 import id_token as _id_token
        app = _make_app(audience="/projects/123/global/backendServices/456")
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(_id_token, "verify_token", side_effect=ValueError("bad token")):
            resp = client.get("/protected", headers={IAP_HEADER: "invalid.jwt.token"})

        assert resp.status_code == 403
        assert "IAP token verification failed" in resp.json()["detail"]

    # ── IAP enabled — valid JWT ───────────────────────────────────────────────

    def test_passes_with_valid_jwt(self):
        from google.oauth2 import id_token as _id_token
        audience = "/projects/123/global/backendServices/456"
        app = _make_app(audience=audience)
        client = TestClient(app)

        fake_claims = {"email": "user@example.com", "sub": "1234567890"}
        with patch.object(_id_token, "verify_token", return_value=fake_claims):
            resp = client.get("/protected", headers={IAP_HEADER: "valid.jwt.token"})

        assert resp.status_code == 200
        assert resp.json() == {"data": "secret"}

    # ── Audience from env var ─────────────────────────────────────────────────

    def test_audience_read_from_env_var(self, monkeypatch):
        monkeypatch.setenv("IAP_AUDIENCE", "/projects/99/global/backendServices/88")
        app = FastAPI()
        app.add_middleware(IAPMiddleware)  # no explicit audience → reads env var

        @app.get("/check")
        def check():
            return {"ok": True}

        # No JWT → 401 (audience was picked up from env)
        resp = TestClient(app).get("/check")
        assert resp.status_code == 401
