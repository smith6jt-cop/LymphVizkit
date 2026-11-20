"""Who is making the request.

dev    single local user "dev". The default; the service also binds to loopback by default.
proxy  the user name arrives in a header set by the authenticating reverse proxy (RC SSO).
       This is only safe when nothing but that proxy can reach the service port, which is
       why it has to be switched on explicitly together with the header name.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request

_VALID = re.compile(r"^[A-Za-z0-9._@\-]{1,128}$")


def current_user(request: Request) -> str:
    state = request.app.state.lymphflow
    settings = state.settings
    if settings.auth_mode == "dev":
        user = "dev"
    else:
        user = (request.headers.get(settings.user_header) or "").strip()
        if not user:
            raise HTTPException(401, "not authenticated: the identity header is missing")
        if not _VALID.match(user):
            raise HTTPException(400, "identity header has an unexpected format")
    state.ensure_user(user)
    return user
