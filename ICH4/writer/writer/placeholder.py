"""Extract {{placeholder}} keys from a Markdown template string."""
from __future__ import annotations

import re

# Matches {{some_key}} — double curly braces, snake_case key
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


def extract_placeholders(template: str) -> list[str]:
    """Return sorted unique placeholder keys found in *template*."""
    return sorted(set(_PLACEHOLDER_RE.findall(template)))


def fill_placeholders(template: str, values: dict[str, str]) -> str:
    """Replace every {{key}} in *template* with values[key].

    Keys not present in *values* are left as ``{{key}} [NOT FILLED]`` so
    reviewers can see exactly what is missing.
    """
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        return f"{{{{{key}}}}} [NOT FILLED]"

    return _PLACEHOLDER_RE.sub(_replace, template)
