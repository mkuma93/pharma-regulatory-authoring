"""
IAP JWT validation middleware.

When Cloud Run sits behind a Google Cloud Load Balancer with IAP enabled,
every request carries an X-Goog-IAP-JWT-Assertion header containing a
signed JWT issued by Google. This middleware verifies that JWT on every
request, rejecting anything that didn't pass through IAP.

Skipped automatically when IAP_AUDIENCE is not set (local dev / non-IAP
deployments) so no code changes are needed between environments.

IAP audience format for backend services:
  /projects/PROJECT_NUMBER/global/backendServices/BACKEND_SERVICE_ID
Set IAP_AUDIENCE in Cloud Run env vars (added by cloudbuild.yaml).
"""

import os

from fastapi import Request
from fastapi.responses import JSONResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.middleware.base import BaseHTTPMiddleware

IAP_HEADER = "x-goog-iap-jwt-assertion"
_http_request = google_requests.Request()


class IAPMiddleware(BaseHTTPMiddleware):
    """Validate the IAP JWT on every request (except /health)."""

    def __init__(self, app, audience: str | None = None):
        super().__init__(app)
        self.audience = audience or os.environ.get("IAP_AUDIENCE", "")

    async def dispatch(self, request: Request, call_next):
        # Always allow health checks (Cloud Run liveness probe bypasses IAP)
        if request.url.path == "/health":
            return await call_next(request)

        # If no audience configured, IAP is disabled — pass through
        if not self.audience:
            return await call_next(request)

        jwt = request.headers.get(IAP_HEADER)
        if not jwt:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing IAP assertion header."},
            )

        try:
            claims = id_token.verify_token(
                jwt,
                _http_request,
                audience=self.audience,
                certs_url="https://www.gstatic.com/iap/verify/public_key",
            )
            # Attach verified identity to request state for use in route handlers
            request.state.iap_email = claims.get("email", "")
            request.state.iap_sub = claims.get("sub", "")
        except Exception as exc:
            return JSONResponse(
                status_code=403,
                content={"detail": f"IAP token verification failed: {exc}"},
            )

        return await call_next(request)
