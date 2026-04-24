"""
template/storage.py

GCS helpers for persisting and loading per-program CTD content templates.
"""
from __future__ import annotations

import json
from datetime import date

from google.cloud import storage

from .models import ProgramInfo, SectionTemplate, TemplateManifest

_client: storage.Client | None = None


def _gcs() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def save_templates(
    bucket_name: str,
    program: ProgramInfo,
    templates: list[SectionTemplate],
) -> TemplateManifest:
    bkt    = _gcs().bucket(bucket_name)
    prefix = program.gcs_prefix
    paths: list[str] = []

    for tmpl in templates:
        gcs_path = f"{prefix}/{tmpl.gcs_path_suffix}"
        blob     = bkt.blob(gcs_path)
        blob.upload_from_string(tmpl.content, content_type="text/markdown; charset=utf-8")
        paths.append(gcs_path)

        # Save the prompt that generated this template alongside the .md
        if tmpl.prompt_messages:
            prompt_path = gcs_path[:-3] + "_prompt.json"  # e.g. module2/2.5_clinical_overview_prompt.json
            bkt.blob(prompt_path).upload_from_string(
                json.dumps({
                    "section_key":  tmpl.section_key,
                    "module_key":   tmpl.module_key,
                    "generated_at": date.today().isoformat(),
                    "messages":     tmpl.prompt_messages,
                }, indent=2),
                content_type="application/json",
            )

    manifest = TemplateManifest(
        program=program,
        generated_date=date.today().isoformat(),
        total_templates=len(templates),
        template_paths=paths,
    )
    manifest_blob = bkt.blob(f"{prefix}/manifest.json")
    manifest_blob.upload_from_string(
        manifest.model_dump_json(indent=2),
        content_type="application/json",
    )
    return manifest


def list_template_paths(bucket_name: str, program: ProgramInfo) -> list[str]:
    bkt    = _gcs().bucket(bucket_name)
    prefix = program.gcs_prefix + "/"
    return [b.name for b in bkt.list_blobs(prefix=prefix) if b.name.endswith(".md")]


def load_template(bucket_name: str, gcs_path: str) -> str:
    return _gcs().bucket(bucket_name).blob(gcs_path).download_as_text()


def load_manifest(bucket_name: str, program: ProgramInfo) -> TemplateManifest | None:
    bkt  = _gcs().bucket(bucket_name)
    blob = bkt.blob(f"{program.gcs_prefix}/manifest.json")
    if not blob.exists():
        return None
    return TemplateManifest(**json.loads(blob.download_as_text()))
