"""
main.py — Falcon FastAPI application factory.

Assembles the app: CORS, lifespan (Mongo warmup + graceful client close),
a friendly global exception handler, health check, and every API router mounted
under the configured prefix (default ``/api``).

Run locally:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("falcon")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate required env vars at startup so the error appears clearly in logs.
    import os
    missing = [k for k in ("OPENROUTER_API_KEY", "MONGODB_URI") if not os.environ.get(k, "").strip()]
    if missing:
        logger.error("MISSING REQUIRED ENV VARS: %s — set them in the platform environment variables.", missing)
    else:
        logger.info("Required env vars verified: OPENROUTER_API_KEY, MONGODB_URI")

    # Warm the Mongo connection (also kicks off async index creation) so the
    # first real request isn't paying connection + index cost. Never fatal:
    # if Atlas is briefly unreachable at boot, requests will retry lazily.
    try:
        from falcon.db import get_db

        get_db()
        logger.info("MongoDB connection warmed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB warmup skipped (will retry lazily): %s", exc)
    yield
    # Graceful shutdown: close the shared MongoClient.
    try:
        from falcon.db import close_db

        close_db()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,  # no auth cookies — keeps "*" origins valid
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError):
        # falcon.identity / falcon.memory raise ValueError for bad input
        # (path-traversal ids, unknown memory types). Surface as 400, not 500.
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "service": "falcon-api", "version": settings.app_version}

    @app.get("/debug-env", tags=["meta"])
    async def debug_env():
        import os
        import falcon.config as Config

        def _mask(v: str) -> str:
            return f"set (…{v[-4:]})" if v and v.strip() else "NOT SET"

        return {
            "OPENROUTER_API_KEY": _mask(os.environ.get("OPENROUTER_API_KEY", "")),
            "OPENAI_API_KEY": _mask(os.environ.get("OPENAI_API_KEY", "")),
            "MONGODB_URI": _mask(os.environ.get("MONGODB_URI", "")),
            # Resolved background-task routing — this is what actually decides
            # whether summary + memory extraction hit OpenAI or OpenRouter.
            "background_use_openai": Config.background_use_openai,
            "openai_background_model": Config.openai_background_model,
            "all_keys": [k for k in os.environ.keys()],
        }

    # ── Routers ────────────────────────────────────────────────────────────
    from app.routers import (
        audit,
        chat,
        config as config_router,
        documents,
        dual_run,
        identities,
        memory,
        testing,
        traces,
        voice,
    )

    prefix = settings.api_prefix
    app.include_router(config_router.router, prefix=prefix)
    app.include_router(identities.router, prefix=prefix)
    app.include_router(chat.router, prefix=prefix)
    app.include_router(memory.router, prefix=prefix)
    app.include_router(traces.router, prefix=prefix)
    app.include_router(audit.router, prefix=prefix)
    app.include_router(dual_run.router, prefix=prefix)
    app.include_router(testing.router, prefix=prefix)
    app.include_router(voice.router, prefix=prefix)
    app.include_router(documents.router, prefix=prefix)

    return app


app = create_app()
