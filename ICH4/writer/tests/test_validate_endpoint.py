"""Tests for POST /validate standalone endpoint."""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("GCP_PROJECT_ID", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api.app import app  # noqa: E402
from writer.models import SectionDocument, ValidationResult  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def _doc_payload(section_key: str, content: str) -> dict:
    return {
        "module_key": "module2",
        "module_label": "CTD Summaries",
        "section_key": section_key,
        "section_label": section_key,
        "content": content,
        "gcs_path": "",
    }


class TestValidateEndpoint:
    def test_empty_documents_returns_422(self):
        resp = client.post("/validate", json={"documents": []})
        assert resp.status_code == 422

    def test_validate_clean_documents(self):
        mock_result = ValidationResult(
            passed=True,
            issues=[],
            summary="All cross-module checks passed.",
        )
        with patch("api.routes.validate.run_validator", return_value=mock_result):
            resp = client.post(
                "/validate",
                json={
                    "documents": [
                        _doc_payload("2.5", "The compound DrugX demonstrated ORR of 68%."),
                        _doc_payload("2.7", "The compound DrugX showed ORR of 68%."),
                    ]
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sections_checked"] == 2
        assert body["validation"]["passed"] is True

    def test_validate_returns_issues(self):
        from writer.models import ValidationIssue
        mock_result = ValidationResult(
            passed=False,
            issues=[
                ValidationIssue(
                    severity="error",
                    section_key="2.5",
                    message="Drug name inconsistency: 'DrugX' vs 'DrugY'.",
                )
            ],
            summary="1 error must be resolved.",
        )
        with patch("api.routes.validate.run_validator", return_value=mock_result):
            resp = client.post(
                "/validate",
                json={
                    "documents": [
                        _doc_payload("2.5", "The compound DrugX."),
                        _doc_payload("2.7", "The compound DrugY."),
                    ]
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["validation"]["passed"] is False
        assert len(body["validation"]["issues"]) == 1
        assert body["validation"]["issues"][0]["severity"] == "error"

    def test_validate_single_doc_still_succeeds(self):
        mock_result = ValidationResult(
            passed=True,
            issues=[],
            summary="Single section — no cross-module check performed.",
        )
        with patch("api.routes.validate.run_validator", return_value=mock_result):
            resp = client.post(
                "/validate",
                json={"documents": [_doc_payload("2.5", "Clean content.")]},
            )
        assert resp.status_code == 200
        assert resp.json()["sections_checked"] == 1
