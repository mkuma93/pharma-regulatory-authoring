"""Index routes — ICH guideline query and per-program section ingest."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from knowledge.indexer import load_index, build_program_index, load_program_index
from knowledge.query_engine import build_query_engine, query

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    ctd_module: str | None = Field(
        default=None,
        description=(
            "Optional CTD module filter for the shared ICH guideline index. "
            "Ignored when program_namespace is set."
        ),
        examples=["2.5", "3", "5"],
    )
    program_namespace: str | None = Field(
        default=None,
        description=(
            "When set, query the per-program generated-content index instead of "
            "the shared ICH guideline index. Format: "
            "'program_{ta}_{dis}_{drug}_{pass_id}', e.g. "
            "'program_neurology_bells_palsy_prednisolone_module5'."
        ),
    )


class QueryResponse(BaseModel):
    question: str
    ctd_module: str | None
    program_namespace: str | None = None
    answer: str


@router.post("/query", response_model=QueryResponse)
def query_index(body: QueryRequest):
    """Ask a question against the ICH guideline index or a per-program content index."""
    try:
        if body.program_namespace:
            index = load_program_index(body.program_namespace)
            engine = build_query_engine(index)  # no ctd_module filter for program indexes
        else:
            index = load_index()
            engine = build_query_engine(index, ctd_module=body.ctd_module)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))

    answer = query(engine, body.question)
    return QueryResponse(
        question=body.question,
        ctd_module=body.ctd_module,
        program_namespace=body.program_namespace,
        answer=answer,
    )


# ── Ingest ────────────────────────────────────────────────────────────────────

class IngestProgramRequest(BaseModel):
    program_namespace: str = Field(
        description=(
            "Unique namespace for this program+pass, e.g. "
            "'program_neurology_bells_palsy_prednisolone_module5'."
        )
    )
    sections: list[dict] = Field(
        description=(
            "List of generated CTD sections to index. Each dict must contain: "
            "module_key (str), section_key (str), content (str)."
        )
    )


class IngestProgramResponse(BaseModel):
    status: str
    namespace: str
    sections_indexed: int


@router.post("/ingest-program", response_model=IngestProgramResponse)
def ingest_program(body: IngestProgramRequest):
    """Index generated CTD sections into a per-program namespace.

    Called by the content worker after each generation pass so that subsequent
    passes (e.g. Module 2.7 after Module 5) can retrieve prior evidence via RAG.
    """
    try:
        build_program_index(body.sections, body.program_namespace)
    except Exception as exc:
        logger.exception("[ingest-program] Failed to build index for %s", body.program_namespace)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    n = len([s for s in body.sections if s.get("content", "").strip()])
    logger.info("[ingest-program] Indexed %d sections → namespace %s", n, body.program_namespace)
    return IngestProgramResponse(
        status="ok",
        namespace=body.program_namespace,
        sections_indexed=n,
    )
