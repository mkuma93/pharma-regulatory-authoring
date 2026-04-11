"""
ICH4 Orchestrator — FastAPI application.

Aggregates two isolated services into a single endpoint:

  1. index-service     →  POST /index/query      (ICH guideline context per module)
  2. template-service  →  POST /generate         (LLM template generation + GCS clinical fetch)

Gradio (ctd_structure/deploy/app.py) calls this service only.

Endpoints:
  GET  /health     — liveness probe
  POST /generate   — full template generation pipeline
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import FastAPI, HTTPException

from config.settings import settings
from models import OrchestratorRequest, OrchestratorResponse, SectionTemplate

logger = logging.getLogger(__name__)

app = FastAPI(
    title="ICH4 Orchestrator",
    version="1.0.0",
    description="Coordinates index, template, and clinical services for CTD template generation.",
)

# ── ICH module query template (same as template/generator._ICH_MODULE_QUERY_TMPL) ──

_ICH_MODULE_QUERY_TMPL = (
    "What are all mandatory contents, required documents, and regulatory "
    "requirements from any applicable ICH guideline (including ICH M4, M4E, "
    "E3, E9, E9-R1, and any other relevant guideline) for {module_key} "
    "({module_label})? "
    "Include requirements for every section and subsection within this module, "
    "covering statistical principles, CSR structure, estimands, and any "
    "guideline-specific obligations where applicable."
)

_MODULES = [
    {"key": "module2", "label": "CTD Summaries",         "ctd_filter": "2"},
    {"key": "module5", "label": "Clinical Study Reports", "ctd_filter": "5"},
]

# Default RAG questions per pass_id; matched against evidence namespace suffix.
_DEFAULT_EVIDENCE_QUERIES: dict[str, str] = {
    "module5": (
        "What were the primary and secondary efficacy endpoint results, responder rates, "
        "study population characteristics (N, demographics), dose, duration, and key "
        "safety findings (AE rates, SAEs, deaths, discontinuations) from the clinical "
        "study reports?"
    ),
    "module2_clinical_summary": (
        "What are the integrated efficacy conclusions (effect sizes, responder rates, "
        "subgroup consistency, NNT) and safety summary (AE profile, SAE rates, "
        "benefit-risk balance) from Clinical Summary sections 2.7.3 and 2.7.4?"
    ),
    "module4": (
        "What are the key nonclinical pharmacology, pharmacokinetics, and toxicology "
        "findings including mechanism of action, species selection, NOAEL derivation, "
        "and genotoxicity/carcinogenicity outcomes?"
    ),
    "module3": (
        "What are the key drug substance and drug product quality characteristics, "
        "BCS classification, stability data, manufacturing process, and specification limits?"
    ),
}


async def _fetch_ich_context(
    client: httpx.AsyncClient,
    module: dict,
) -> tuple[str, str | None]:
    """Query the index service for one module. Returns (module_key, answer | None)."""
    question = _ICH_MODULE_QUERY_TMPL.format(
        module_key=module["key"], module_label=module["label"]
    )
    try:
        r = await client.post(
            f"{settings.index_service_url}/index/query",
            json={"question": question, "ctd_module": module["ctd_filter"]},
            timeout=settings.timeout_seconds,
        )
        if r.status_code == 200:
            return module["key"], r.json().get("answer")
    except Exception as exc:
        logger.warning("[orchestrator] index query failed for %s: %s", module["key"], exc)
    return module["key"], None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate", response_model=OrchestratorResponse)
async def generate(body: OrchestratorRequest) -> OrchestratorResponse:
    """Coordinate index and template services to generate CTD templates."""

    async with httpx.AsyncClient() as client:

        # ── Determine active modules (apply module_filter if set) ──────────────
        active_modules = (
            [m for m in _MODULES if m["key"] in body.module_filter]
            if body.module_filter
            else _MODULES
        )

        # ── Step 1: Fetch ICH guideline context per active module in parallel ──
        tasks: list = []

        if body.include_ich_context:
            for module in active_modules:
                tasks.append(_fetch_ich_context(client, module))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # ── Parse ICH context results ──────────────────────────────────────────
        ich_context: dict[str, str] = {}

        if body.include_ich_context:
            for idx, module in enumerate(active_modules):
                result = results[idx]
                if not isinstance(result, Exception):
                    module_key, answer = result
                    if answer:
                        ich_context[module_key] = answer

        # ── Step 1.5: Query per-program index for prior-pass evidence ──────────
        # ICH M4E(R2) evidence chain: Module 5 CSRs → Module 2.7 Summary →
        # Module 2.5 Overview.  Each pass indexes its output so downstream passes
        # retrieve actual generated content rather than hallucinating statistics.
        prior_evidence: dict[str, str] = {}

        if body.evidence_namespaces:
            for ns in body.evidence_namespaces:
                question = next(
                    (v for k, v in _DEFAULT_EVIDENCE_QUERIES.items() if ns.endswith(f"_{k}")),
                    "Summarise the key findings from this module.",
                )
                try:
                    r = await client.post(
                        f"{settings.index_service_url}/index/query",
                        json={"question": question, "program_namespace": ns},
                        timeout=settings.timeout_seconds,
                    )
                    if r.status_code == 200:
                        answer = r.json().get("answer", "")
                        if answer:
                            prior_evidence[ns] = answer
                            logger.info(
                                "[orchestrator] Prior evidence from %s (%d chars)",
                                ns, len(answer),
                            )
                    else:
                        logger.warning(
                            "[orchestrator] Program index query %s returned HTTP %s",
                            ns, r.status_code,
                        )
                except Exception as exc:
                    logger.warning(
                        "[orchestrator] Program index query failed for %s: %s", ns, exc
                    )

        # ── Step 2: Call template service (fetches clinical from GCS itself) ──
        payload: dict = {
            "program": body.program.model_dump(),
            "ich_context": ich_context if ich_context else None,
            "include_clinical_data": body.include_clinical_data,
            "section_key_prefixes": body.section_key_prefixes,
            "prior_evidence": prior_evidence if prior_evidence else None,
        }

        try:
            r = await client.post(
                f"{settings.template_service_url}/generate",
                json=payload,
                timeout=settings.timeout_seconds,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Template service error: {exc.response.text}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Template service unreachable: {exc}",
            ) from exc

        data = r.json()

    return OrchestratorResponse(
        templates=[SectionTemplate(**t) for t in data["templates"]],
        modules_generated=data["modules_generated"],
        ich_index_used=data["ich_index_used"],
        clinical_data_used=data["clinical_data_used"],
    )
