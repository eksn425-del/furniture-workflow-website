from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class InternalServiceAuthMiddleware(BaseHTTPMiddleware):
    """Protect the control API when it is reachable outside the web container."""

    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path == "/api/v1/health":
            return await call_next(request)
        supplied = request.headers.get("x-internal-service-token", "")
        if not secrets.compare_digest(supplied, self.token):
            return JSONResponse({"detail": "internal service authentication required"}, status_code=401)
        return await call_next(request)
