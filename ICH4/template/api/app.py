"""
ICH4 Template Service — FastAPI application.

Endpoints:
  GET  /health         — liveness probe
  POST /generate       — generate CTD section templates for a program
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import generate
from config.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.gcp_project_id:
        print(f"[startup] Secrets loaded from GCP Secret Manager (project: {settings.gcp_project_id}).")
        if not settings.openai_api_key:
            raise RuntimeError("[startup] OPENAI_API_KEY not found in Secret Manager.")
        print("[startup] OPENAI_API_KEY present.")
    else:
        print("[startup] GCP_PROJECT_ID not set — using local env / .env file.")
    yield


app = FastAPI(
    title="ICH4 Template Service",
    version="1.0.0",
    description="LLM-based CTD section template generation for drug programs.",
    lifespan=lifespan,
)

app.include_router(generate.router, prefix="", tags=["templates"])


@app.get("/health")
def health():
    return {"status": "ok"}
