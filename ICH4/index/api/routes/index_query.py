"""POST /index/query — free-text question against the ICH CTD LlamaIndex."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from knowledge.indexer import load_index
from knowledge.query_engine import build_query_engine, query

router = APIRouter()


class QueryRequest(BaseModel):
    question: str
    ctd_module: str | None = Field(
        default=None,
        description=(
            "Optional CTD module filter. Restrict the search to a specific module "
            "or sub-section (e.g. '2', '2.5', '3', '4', '5'). "
            "Leave empty to search across all indexed CTD guidelines."
        ),
        examples=["2.5", "3", "5"],
    )


class QueryResponse(BaseModel):
    question: str
    ctd_module: str | None
    answer: str


@router.post("/query", response_model=QueryResponse)
def query_index(body: QueryRequest):
    """Ask a free-text question against the pre-built ICH CTD guideline index."""
    try:
        index = load_index()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))

    engine = build_query_engine(index, ctd_module=body.ctd_module)
    answer = query(engine, body.question)
    return QueryResponse(question=body.question, ctd_module=body.ctd_module, answer=answer)
