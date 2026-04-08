"""Writer service configuration.

Priority chain (highest → lowest):
  1. pydantic init kwargs
  2. GCP Secret Manager (if GCP_PROJECT_ID is set)
  3. File-based secrets  (config/secrets/<FIELD_NAME>)
  4. Environment variables
  5. .env file
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

CONFIG_DIR = Path(__file__).resolve().parent   # ICH4/writer/config/


class GcpSecretManagerSettingsSource(PydanticBaseSettingsSource):
    """Load settings from GCP Secret Manager."""

    def __init__(self, settings_cls: type[BaseSettings], project_id: str | None = None) -> None:
        super().__init__(settings_cls)
        self._project_id: str = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self._client = None
        if self._project_id:
            try:
                from google.cloud import secretmanager  # noqa: PLC0415
                self._client = secretmanager.SecretManagerServiceClient()
            except Exception:
                pass

    def _fetch_secret(self, secret_name: str) -> str | None:
        if not self._client or not self._project_id:
            return None
        resource = f"projects/{self._project_id}/secrets/{secret_name}/versions/latest"
        try:
            from google.api_core.exceptions import NotFound, PermissionDenied  # noqa: PLC0415
            response = self._client.access_secret_version(request={"name": resource})
            return response.payload.data.decode("utf-8").strip()
        except (NotFound, PermissionDenied):
            return None
        except Exception:
            return None

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        value = self._fetch_secret(field_name.upper())
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field_info, field_name)
            if value is not None:
                data[key] = value
        return data


class Settings(BaseSettings):
    gcp_project_id: str = Field(default="", description="GCP project ID")

    # Required for LLM calls
    openai_api_key: str = Field(..., description="OpenAI API key")

    # LLM config
    llm_model: str = Field(default="gpt-4o")
    llm_temperature: float = Field(default=0.1)

    # GCS bucket holding templates + clinical data
    gcs_bucket_name: str = Field(default="")

    model_config = {
        "env_file": str(CONFIG_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "secrets_dir": str(CONFIG_DIR / "secrets"),
        "extra": "ignore",
    }

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        gcp_sm = GcpSecretManagerSettingsSource(settings_cls)
        return (init_settings, gcp_sm, file_secret_settings, env_settings, dotenv_settings)


settings = Settings()
