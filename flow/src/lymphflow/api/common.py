"""Shared FastAPI dependencies and the domain-error -> HTTP mapping."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..auth import current_user
from ..core.comp import CompError
from ..core.engine import CompensationInvalid, PanelMismatch
from ..core.fcs_ingest import FcsError
from ..core.gates import GateError
from ..core.scales import ScaleError
from ..state import AppState, Conflict, NotFound

User = Depends(current_user)


def get_state(request: Request) -> AppState:
    return request.app.state.lymphflow


State = Depends(get_state)


def install_error_handlers(app: FastAPI) -> None:
    def handler(status: int, kind: str):
        async def _h(_: Request, exc: Exception):
            body = {"detail": str(exc), "error": kind}
            if isinstance(exc, PanelMismatch):
                body["missing_channels"] = exc.missing
            return JSONResponse(body, status_code=status)

        return _h

    app.add_exception_handler(NotFound, handler(404, "not_found"))
    app.add_exception_handler(Conflict, handler(409, "conflict"))
    # A template applied to a sample with a different panel fails for that sample, visibly.
    app.add_exception_handler(PanelMismatch, handler(409, "panel_mismatch"))
    app.add_exception_handler(CompensationInvalid, handler(409, "compensation_invalid"))
    app.add_exception_handler(ScaleError, handler(422, "invalid_scale"))
    app.add_exception_handler(GateError, handler(422, "invalid_gate"))
    app.add_exception_handler(CompError, handler(422, "invalid_compensation"))
    app.add_exception_handler(FcsError, handler(422, "invalid_fcs"))
