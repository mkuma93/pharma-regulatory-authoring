"""Tests for POST /generate — downstream HTTP calls are mocked."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

_PROGRAM_PAYLOAD = {
    "therapeutic_area": "oncology",
    "disease_type": "lung cancer",
    "drug_name": "testdrug",
}

_TEMPLATE_RESPONSE = {
    "templates": [
        {
            "module_key": "module2",
            "module_label": "CTD Summaries",
            "section_key": "2.3_quality_overall_summary",
            "section_label": "Quality Overall Summary",
            "content": "## QOS",
        }
    ],
    "modules_generated": ["module2"],
    "ich_index_used": True,
    "clinical_data_used": False,
}


def _make_async_client_mock(index_answer="ICH guideline text", template_status=200):
    """Build a mock that satisfies `async with httpx.AsyncClient() as client:`."""

    _dummy_request = httpx.Request("POST", "http://test")

    async def _post(url, **kwargs):
        if "/index/query" in url:
            r = httpx.Response(200, json={"answer": index_answer}, request=_dummy_request)
            return r
        if "/generate" in url:
            r = httpx.Response(template_status, json=_TEMPLATE_RESPONSE, request=_dummy_request)
            return r
        return httpx.Response(404, json={}, request=_dummy_request)

    inner = AsyncMock()
    inner.post = AsyncMock(side_effect=_post)

    # httpx.AsyncClient() returns instance; `async with instance` calls __aenter__
    instance = MagicMock()
    instance.__aenter__ = AsyncMock(return_value=inner)
    instance.__aexit__ = AsyncMock(return_value=False)

    cls_mock = MagicMock(return_value=instance)
    return cls_mock


def test_generate_success():
    with patch("main.httpx.AsyncClient", new=_make_async_client_mock()):
        resp = client.post(
            "/generate",
            json={
                "program": _PROGRAM_PAYLOAD,
                "include_ich_context": True,
                "include_clinical_data": False,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "templates" in data
    assert len(data["templates"]) >= 1


def test_generate_without_ich_context():
    with patch("main.httpx.AsyncClient", new=_make_async_client_mock()):
        resp = client.post(
            "/generate",
            json={
                "program": _PROGRAM_PAYLOAD,
                "include_ich_context": False,
                "include_clinical_data": False,
            },
        )
    assert resp.status_code == 200


def test_generate_template_service_502():
    with patch("main.httpx.AsyncClient", new=_make_async_client_mock(template_status=500)):
        resp = client.post(
            "/generate",
            json={
                "program": _PROGRAM_PAYLOAD,
                "include_ich_context": False,
                "include_clinical_data": False,
            },
        )
    assert resp.status_code == 502
