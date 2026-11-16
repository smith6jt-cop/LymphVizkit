"""FastAPI application factory.

Everything is served relative to the document base, so the same build works at `/`, behind
a prefix-stripping proxy route (`/flow/` -> port), or with LYMPH_FLOW_URL_PREFIX when the
proxy forwards the prefix. No asset is fetched from another host.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import experiments, gates, jobs, plots
from .api.common import install_error_handlers
from .config import Settings, load_settings
from .jobs.dispatcher import Dispatcher
from .state import AppState

WEB_DIR = Path(__file__).parent / "web"

SECURITY_HEADERS = {
    # Same-origin only; no inline or remote scripts. 'unsafe-inline' is limited to styles.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _build(settings: Settings) -> FastAPI:
    state = AppState(settings)
    state.dispatcher = Dispatcher(state)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state.dispatcher.start()
        yield
        state.dispatcher.stop()

    app = FastAPI(title="LymphVizkit Flow", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.lymphflow = state
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.endswith(("/api/docs", "/api/openapi.json")):
            for k, v in SECURITY_HEADERS.items():
                response.headers.setdefault(k, v)
        return response

    install_error_handlers(app)
    for module in (experiments, plots, gates, jobs):
        app.include_router(module.router, prefix="/api")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    inner = _build(settings)
    if not settings.url_prefix:
        return inner

    # Serve under a prefix (proxy forwards the prefix unchanged). `/flow` -> `/flow/` so that
    # relative URLs in the page resolve under the prefix.
    outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=inner.router.lifespan_context)
    outer.state.lymphflow = inner.state.lymphflow

    @outer.get(settings.url_prefix, include_in_schema=False)
    async def _slash():
        return RedirectResponse(settings.url_prefix + "/", status_code=308)

    outer.mount(settings.url_prefix, inner)
    return outer
