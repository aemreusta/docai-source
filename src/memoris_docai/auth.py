from __future__ import annotations

import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class ServiceAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = os.environ.get("DOCAI_SERVICE_TOKEN", "")
            local = os.environ.get("MEMORIS_ENV", "local") in ("local", "dev")
            if scope["path"] == "/healthz":
                if not token and not local:
                    await JSONResponse({"status": "unavailable"}, status_code=503)(scope, receive, send)
                    return
            elif token or not local:
                values = Headers(scope=scope).getlist("authorization")
                if not token or len(values) != 1 or not hmac.compare_digest(
                    values[0].encode("latin-1"), f"Bearer {token}".encode()
                ):
                    response = JSONResponse(
                        {"code": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
