"""
Tests for api/routes/index_query.py

Covers:
  - QueryRequest / QueryResponse Pydantic models
  - POST /index/query — 200 with mocked knowledge layer
  - POST /index/query — ctd_module filter forwarded to build_query_engine
  - POST /index/query — 503 when index not on disk
  - POST /index/query — 422 for invalid request body
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.index_query import QueryRequest, QueryResponse, router


# ── Pydantic model tests ──────────────────────────────────────────────────────

class TestQueryRequest:
    def test_requires_question(self):
        with pytest.raises(Exception):
            QueryRequest()  # type: ignore[call-arg]

    def test_ctd_module_optional(self):
        req = QueryRequest(question="What is CTD?")
        assert req.ctd_module is None

    def test_ctd_module_set(self):
        req = QueryRequest(question="What is 2.5?", ctd_module="2.5")
        assert req.ctd_module == "2.5"


class TestQueryResponse:
    def test_all_fields(self):
        resp = QueryResponse(question="Q?", ctd_module="3", answer="A")
        assert resp.question == "Q?"
        assert resp.ctd_module == "3"
        assert resp.answer == "A"

    def test_ctd_module_nullable(self):
        resp = QueryResponse(question="Q?", ctd_module=None, answer="A")
        assert resp.ctd_module is None


# ── Route handler tests ───────────────────────────────────────────────────────

def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/index")
    return app


class TestQueryRoute:
    @pytest.fixture
    def client(self):
        return TestClient(_app_with_router())

    def test_returns_200_with_answer(self, client):
        mock_index = MagicMock()
        mock_engine = MagicMock()
        with (
            patch("api.routes.index_query.load_index", return_value=mock_index),
            patch("api.routes.index_query.build_query_engine", return_value=mock_engine),
            patch("api.routes.index_query.query", return_value="ICH M4 answer"),
        ):
            resp = client.post("/index/query", json={"question": "What is CTD?"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "ICH M4 answer"
        assert data["question"] == "What is CTD?"
        assert data["ctd_module"] is None

    def test_ctd_module_forwarded_to_engine(self, client):
        mock_index = MagicMock()
        mock_engine = MagicMock()
        with (
            patch("api.routes.index_query.load_index", return_value=mock_index),
            patch("api.routes.index_query.build_query_engine", return_value=mock_engine) as mock_build,
            patch("api.routes.index_query.query", return_value="filtered answer"),
        ):
            resp = client.post(
                "/index/query",
                json={"question": "Clinical overview?", "ctd_module": "2.5"},
            )

        assert resp.status_code == 200
        mock_build.assert_called_once_with(mock_index, ctd_module="2.5")
        assert resp.json()["ctd_module"] == "2.5"

    def test_returns_503_when_index_not_found(self, client):
        with patch(
            "api.routes.index_query.load_index",
            side_effect=FileNotFoundError("No index found at /tmp/index_store"),
        ):
            resp = client.post("/index/query", json={"question": "anything"})

        assert resp.status_code == 503
        assert "No index found" in resp.json()["detail"]

    def test_returns_422_for_missing_question(self, client):
        resp = client.post("/index/query", json={})
        assert resp.status_code == 422

    def test_question_echoed_in_response(self, client):
        question = "List all sections in Module 3"
        mock_index = MagicMock()
        mock_engine = MagicMock()
        with (
            patch("api.routes.index_query.load_index", return_value=mock_index),
            patch("api.routes.index_query.build_query_engine", return_value=mock_engine),
            patch("api.routes.index_query.query", return_value="some answer"),
        ):
            resp = client.post("/index/query", json={"question": question})

        assert resp.json()["question"] == question
