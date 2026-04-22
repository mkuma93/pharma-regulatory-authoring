"""
ICH4 Content Pipeline — FastAPI application.

Scope: ICH4 content generation only.
Aggregates two backend services into a single /generate endpoint:

  1. index-service     →  POST /index/query  (ICH guideline context per module)
  2. template-service  →  POST /generate     (LLM template generation + GCS clinical fetch)

This service does NOT make routing decisions between features (CTD scaffolding,
content generation, data analysis). Global intent routing is handled by
ui/coordinator.py in the Gradio UI layer.

Endpoints:
  GET  /health     — liveness probe
  POST /generate   — full template generation pipeline
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException

from config.settings import settings
from models import OrchestratorRequest, OrchestratorResponse, SectionTemplate


# ── OIDC helpers (Cloud Run service-to-service auth) ──────────────────────────

def _service_audience(base_url: str) -> str:
    """Extract scheme+host from a URL to use as OIDC audience."""
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def _oidc_headers(audience: str) -> dict[str, str]:
    """Return Authorization header with an OIDC token for Cloud Run service calls."""
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2.id_token import fetch_id_token
        token = fetch_id_token(AuthRequest(), audience)
        return {"Authorization": f"Bearer {token}"}
    except Exception as exc:
        logger.warning("[pipeline] Could not obtain OIDC token for %s: %s", audience, exc)
        return {}

logger = logging.getLogger(__name__)

app = FastAPI(
    title="ICH4 Content Pipeline",
    version="1.0.0",
    description="Aggregates ICH index and template services for CTD content generation.",
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
    headers: dict[str, str],
) -> tuple[str, str | None]:
    """Query the index service for one module. Returns (module_key, answer | None)."""
    question = _ICH_MODULE_QUERY_TMPL.format(
        module_key=module["key"], module_label=module["label"]
    )
    try:
        r = await client.post(
            f"{settings.index_service_url}/index/query",
            json={"question": question},
            headers=headers,
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

    index_headers    = _oidc_headers(_service_audience(settings.index_service_url))
    template_headers = _oidc_headers(_service_audience(settings.template_service_url))

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
                tasks.append(_fetch_ich_context(client, module, index_headers))

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
                        headers=index_headers,
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
            "bucket": body.bucket,
        }

        try:
            r = await client.post(
                f"{settings.template_service_url}/generate",
                json=payload,
                headers=template_headers,
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

    # ── Step 3: Pre-resolve template placeholders via clinical-analyst ────────
    # This is the right place to resolve because we just built the templates and
    # know exactly which {{keys}} they contain. The worker receives resolved_values
    # in the response and passes them straight to the writer — no separate B.5 call.
    resolved_values: dict[str, str] = {}
    analyst_url = settings.clinical_analyst_service_url.rstrip("/")
    if analyst_url and body.bucket:
        placeholder_keys: set[str] = set()
        _ph_re = __import__("re").compile(r"\{\{(\w+)\}\}")
        for tmpl in data.get("templates", []):
            placeholder_keys.update(_ph_re.findall(tmpl.get("content", "")))

        if placeholder_keys:
            analyst_headers = _oidc_headers(_service_audience(analyst_url))
            try:
                async with httpx.AsyncClient() as analyst_client:
                    ra = await analyst_client.post(
                        f"{analyst_url}/resolve",
                        headers=analyst_headers,
                        json={
                            "therapeutic_area": body.program.therapeutic_area,
                            "disease_type":     body.program.disease_type,
                            "drug_name":        body.program.drug_name,
                            "bucket":           body.bucket,
                            "placeholder_keys": sorted(placeholder_keys),
                        },
                        timeout=settings.timeout_seconds,
                    )
                    if ra.status_code == 200:
                        resolved_values = ra.json().get("resolved_values", {})
                        logger.info(
                            "[orchestrator] Pre-resolved %d/%d placeholder keys",
                            len(resolved_values), len(placeholder_keys),
                        )
                    elif ra.status_code == 404:
                        logger.info("[orchestrator] No clinical manifest — skipping /resolve")
                    else:
                        logger.warning(
                            "[orchestrator] clinical-analyst /resolve HTTP %s — continuing",
                            ra.status_code,
                        )
            except Exception as exc:
                logger.warning("[orchestrator] /resolve failed (non-fatal): %s", exc)

    return OrchestratorResponse(
        templates=[SectionTemplate(**t) for t in data["templates"]],
        modules_generated=data["modules_generated"],
        ich_index_used=data["ich_index_used"],
        clinical_data_used=data["clinical_data_used"],
        resolved_values=resolved_values,
    )
