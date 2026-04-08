"""
template
────────
Per-program CTD section content template generator.

Generates Markdown templates for every ICH M4 CTD section, personalised
to a drug / indication / therapeutic area, using an LLM and stored in GCS.

Typical usage
─────────────
    from template import ProgramInfo, generate_program_templates, save_templates

    program = ProgramInfo(
        therapeutic_area="oncology",
        disease_type="lung_cancer",
        drug_name="carboplatin",
    )

    # ctd_output: CTDStructureOutput from the extraction pipeline
    templates = generate_program_templates(program, ctd_output)
    manifest  = save_templates(bucket_name, program, templates)

    print(f"Generated {manifest.total_templates} templates → {program.gcs_prefix}/")

GCS layout
──────────
    therapeutic-area/{ta}/{disease}/{drug}/templates/
        manifest.json
        {module_key}/{section_key}.md
"""
from .generator import generate_program_templates
from .models import ProgramInfo, SectionTemplate, TemplateManifest
from .storage import list_template_paths, load_manifest, load_template, save_templates

__all__ = [
    "ProgramInfo",
    "SectionTemplate",
    "TemplateManifest",
    "generate_program_templates",
    "save_templates",
    "list_template_paths",
    "load_template",
    "load_manifest",
]
