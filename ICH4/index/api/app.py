"""
Phase 1 FastAPI application — ICH CTD guideline index query API.

On cold start (Cloud Run):
  Downloads pre-built LlamaIndex from GCS → /tmp/index_store

Endpoints:
  GET  /health          — Cloud Run liveness probe
  POST /index/query     — ask any free-text question against the ICH CTD index,
                          with optional filtering by CTD module (e.g. "2.5", "3", "5")
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI

from api.middleware.iap import IAPMiddleware
from api.routes import index_query
from config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.gcp_project_id:
        print(f"[startup] Secrets loaded from GCP Secret Manager (project: {settings.gcp_project_id}).")
        # Validate that required keys were actually fetched
        missing = [k for k, v in {
            "OPENAI_API_KEY": settings.openai_api_key,
            "LLAMA_CLOUD_API_KEY": settings.llama_cloud_api_key,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"[startup] Required secrets not found in Secret Manager: {missing}")
        print(f"[startup] All required secrets present.")
    else:
        print("[startup] GCP_PROJECT_ID not set — using local env / .env file for secrets.")

    if settings.gcs_bucket_name:
        from api.gcs import download_index_from_gcs
        print(f"[startup] Downloading index from gs://{settings.gcs_bucket_name}/...")
        download_index_from_gcs()
        print("[startup] Index ready.")
    else:
        print("[startup] GCS_BUCKET_NAME not set — using local index.")
    yield


app = FastAPI(
    title="ICH CTD Guideline Index API",
    version="1.0.0",
    description=(
        "Query ICH CTD (Common Technical Document) guidelines via a LlamaIndex-powered REST API. "
        "Supports optional filtering by CTD module (2, 2.5, 3, 4, 5)."
    ),
    lifespan=lifespan,
)

# IAP JWT validation — active only when IAP_AUDIENCE env var is set.
# Skipped automatically in local dev (IAP_AUDIENCE not set).
app.add_middleware(IAPMiddleware)

app.include_router(index_query.router,  prefix="/index",     tags=["index"])


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
