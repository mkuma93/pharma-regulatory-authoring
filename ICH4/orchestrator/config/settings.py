import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

CONFIG_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # URLs for the three internal services
    # Override via env vars or .env file before deploying
    index_service_url: str = Field(
        default="https://ich4-index-811317821863.us-central1.run.app",
        description="Base URL of the ICH4 index (RAG) service",
    )
    template_service_url: str = Field(
        default="http://localhost:8082",
        description="Base URL of the ICH4 template generation service",
    )

    # HTTP timeout for calls to internal services (seconds)
    timeout_seconds: float = Field(default=300.0)

    # GCP project (used to obtain identity tokens for service-to-service calls)
    gcp_project_id: str = Field(default="")

    model_config = {
        "env_file": str(CONFIG_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
