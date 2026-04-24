"""Identity-Aware Proxy (IAP) identity helpers.

Cloud Run + IAP injects the authenticated end-user's email as an HTTP header:

    X-Goog-Authenticated-User-Email: accounts.google.com:alice@example.com

This module centralizes extraction of that identity so services can attribute
audit events to the real human instead of the runtime service account.

Services import this via the Dockerfile COPY pattern (same as bq_audit.py).
At local-dev time the helpers gracefully return "" when the header is absent.
"""

from __future__ import annotations

import os
from typing import Mapping


_HDR = "x-goog-authenticated-user-email"

# When ENFORCE_IAP=true (set on Cloud Run), requests without a valid IAP
# identity header are rejected.  Defaults to false for local development.
ENFORCE_IAP: bool = os.getenv("ENFORCE_IAP", "false").lower() == "true"


def iap_user_from_headers(headers: Mapping[str, str] | None) -> str:
    """Return the bare email from the IAP header, or "" if absent.

    Accepts any case-insensitive header mapping (FastAPI `request.headers`,
    a plain dict, etc.). Strips the ``accounts.google.com:`` prefix that
    Google prepends when the identity comes from Google accounts.
    """
    if not headers:
        return ""
    # Starlette/FastAPI Headers is already case-insensitive; for a plain dict
    # try a few casings.
    raw = ""
    try:
        raw = headers.get(_HDR, "") or headers.get(_HDR.title(), "") \
            or headers.get("X-Goog-Authenticated-User-Email", "")
    except Exception:
        raw = ""
    if not raw:
        return ""
    # Format is "<issuer>:<email>" — keep only the email part.
    return raw.split(":", 1)[-1].strip() if ":" in raw else raw.strip()


def resolve_author(body_author: str, headers: Mapping[str, str] | None) -> str:
    """Return the effective author for an incoming request.

    Priority:
      1. Explicit ``body_author`` from the request payload (if non-empty).
      2. IAP-authenticated user email from the request headers.
      3. Empty string (caller may substitute "system" for display).

    When ``ENFORCE_IAP`` is ``True`` a missing IAP identity raises
    ``PermissionError`` so callers can surface it as an HTTP 403.
    """
    clean = (body_author or "").strip()
    if clean:
        return clean
    identity = iap_user_from_headers(headers)
    if not identity and ENFORCE_IAP:
        raise PermissionError(
            "IAP identity header is required but was not present. "
            "Ensure Cloud Run IAP is configured and the request is authenticated."
        )
    return identity
