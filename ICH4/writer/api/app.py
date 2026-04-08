"""
ICH4 Writer Service — FastAPI application.

Endpoints:
  GET  /health                — liveness probe
  POST /write                 — generate CTD documents from templates + clinical data
  POST /clinical-data/upload  — upload CSV for a program/drug and auto-build manifest
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import upload as upload_route
from api.routes import validate as validate_route
from api.routes import write as write_route
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
    title="ICH4 Writer Service",
    version="1.0.0",
    description="LLM-powered CTD document generation with cross-module validation.",
    lifespan=lifespan,
)

app.include_router(write_route.router, prefix="", tags=["writer"])
app.include_router(validate_route.router, prefix="", tags=["validator"])
app.include_router(upload_route.router, prefix="", tags=["clinical-data"])


@app.get("/health")
def health():
    return {"status": "ok"}
