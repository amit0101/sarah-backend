"""Sarah FastAPI application entry."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import admin, chat, internal, webhooks, ws_chat
from app.config import get_settings
from app.conversation_engine.engine import warmup_openai_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Sarah API starting v%s", __version__)
    # Session 21 — warm the shared AsyncOpenAI client's connection pool so the
    # first real user turn after a Render cold start doesn't pay the TLS /
    # HTTP/2 handshake cost (~300-500 ms). Non-blocking if it fails.
    try:
        await warmup_openai_client()
    except Exception:
        logger.exception("openai_warmup crashed; continuing startup")
    yield
    logger.info("Sarah API shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins == ["*"] or not origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled exception on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {str(exc)}"},
        )

    app.include_router(chat.router)
    app.include_router(ws_chat.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(internal.router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "Sarah API", "version": __version__}

    return app


app = create_app()
