import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

BASE_DIR = Path(__file__).resolve().parent.parent   # ICH4/index/
CONFIG_DIR = Path(__file__).resolve().parent         # ICH4/index/config/


class GcpSecretManagerSettingsSource(PydanticBaseSettingsSource):
    """Load settings from GCP Secret Manager.

    Each secret must be stored with a name matching the uppercased field name:
        openai_api_key      →  projects/{project}/secrets/OPENAI_API_KEY/versions/latest
        llama_cloud_api_key →  projects/{project}/secrets/LLAMA_CLOUD_API_KEY/versions/latest

    Behaviour:
    - Reads GCP_PROJECT_ID from the environment to locate secrets.
    - Falls back silently if Secret Manager is unreachable or a secret doesn't exist.
    - On Cloud Run the service account must have the secretmanager.secretAccessor role.
    - Locally, set GCP_PROJECT_ID= (empty) to skip Secret Manager entirely.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        project_id: str | None = None,
    ) -> None:
        super().__init__(settings_cls)
        self._project_id: str = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self._client = None

        if self._project_id:
            try:
                from google.cloud import secretmanager  # noqa: PLC0415
                self._client = secretmanager.SecretManagerServiceClient()
            except Exception:
                # ADC not configured or library not installed — skip silently
                pass

    def _fetch_secret(self, secret_name: str) -> str | None:
        """Access the latest version of a secret. Returns None on any failure."""
        if not self._client or not self._project_id:
            return None
        resource = (
            f"projects/{self._project_id}/secrets/{secret_name}/versions/latest"
        )
        try:
            from google.api_core.exceptions import NotFound, PermissionDenied  # noqa: PLC0415
            response = self._client.access_secret_version(request={"name": resource})
            return response.payload.data.decode("utf-8").strip()
        except (NotFound, PermissionDenied):
            return None
        except Exception:
            return None

    def get_field_value(
        self, field: Any, field_name: str
    ) -> tuple[Any, str, bool]:
        # Secret name convention: uppercase field name (OPENAI_API_KEY)
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
    # === GCP project (used by GcpSecretManagerSettingsSource) ===
    gcp_project_id: str = Field(default="", description="GCP project ID")

    # === Required API keys ===
    openai_api_key: str = Field(..., description="OpenAI API key")
    llama_cloud_api_key: str = Field(..., description="LlamaCloud API key for LlamaParse")

    # === LLM config ===
    llm_model: str = Field(default="gpt-4o")
    embedding_model: str = Field(default="text-embedding-3-small")

    # === LlamaParse config ===
    llamaparse_result_type: str = Field(default="markdown")
    llamaparse_verbose: bool = Field(default=True)

    # === Paths ===
    guidelines_dir: Path = Field(default=BASE_DIR / "guidelines")
    index_persist_dir: Path = Field(default=BASE_DIR / "data" / "index_store")

    # === GCS (required on Cloud Run) ===
    gcs_bucket_name: str = Field(default="")
    gcs_index_prefix: str = Field(default="index_store")

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
        # Priority (highest → lowest):
        #   init args → GCP Secret Manager → mounted file secrets → OS env vars → .env file
        gcp_sm = GcpSecretManagerSettingsSource(settings_cls)
        return (init_settings, gcp_sm, file_secret_settings, env_settings, dotenv_settings)


settings = Settings()
