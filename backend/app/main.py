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

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.deps import require_admin, require_user
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

        # Prove storage actually reads and writes before anything downstream
        # relies on it. Deliberately first and deliberately loud: a warmed
        # connection only means a handle was created, and the failure this
        # guards against — storage that looks fine while silently losing data
        # on redeploy — is invisible until someone notices the assistant has
        # forgotten something it claimed to remember. Logged at ERROR on
        # failure so it cannot be mistaken for routine startup chatter.
        try:
            from falcon.memory_bridge import startup_report

            report = startup_report()
            (logger.info if "OK" in report.split("—")[0] else logger.error)(report)
        except Exception as bridge_exc:  # noqa: BLE001
            logger.error(
                "MEMORY BRIDGE could not run at startup: %s. Persistence is UNVERIFIED.",
                bridge_exc,
            )

        # Seed the first admin account on a fresh database (no-op if one exists).
        #
        # Credentials come from the environment. They used to be literals in this
        # file — anyone with the source, which includes anyone who has ever seen
        # the repository, knew the admin password of every deployment that had
        # not changed it, and there was nothing to prompt anyone to change it.
        # No credentials configured means no seeding: an empty user table is a
        # visible problem with a documented fix, a known password is neither.
        try:
            from falcon.admin_users import seed_first_admin

            seed_user = os.environ.get("FALCON_ADMIN_USERNAME", "").strip()
            seed_pass = os.environ.get("FALCON_ADMIN_PASSWORD", "")
            if seed_user and seed_pass:
                seed_first_admin(seed_user, seed_pass)
                logger.info("Admin seed check complete")
            else:
                from falcon.db import get_db as _get_db

                if _get_db()["admin_users"].count_documents({}) == 0:
                    logger.error(
                        "No admin accounts exist and FALCON_ADMIN_USERNAME / "
                        "FALCON_ADMIN_PASSWORD are not set — nobody can log in. "
                        "Set both and restart to create the first admin."
                    )
        except Exception as seed_exc:
            logger.warning("Admin seed skipped: %s", seed_exc)

        # Load dynamically spawned watcher tools before any watcher thread can
        # dispatch, so a tool created in an earlier run is available immediately
        # rather than only after dispatch's self-healing lookup.
        try:
            import falcon.watcher_tools as watcher_tools
            from falcon.watcher_generated import ensure_loaded

            ensure_loaded()
            # Logged with the pid so a stale worker (uvicorn --reload on Windows
            # can fail to replace it) is obvious when comparing against
            # GET /api/watcher/debug.
            logger.info(
                "Watcher tools ready in pid %s: %s",
                os.getpid(), ", ".join(watcher_tools.list_tools()),
            )
        except Exception as gen_exc:
            logger.warning("Generated watcher tools not loaded: %s", gen_exc)

        # Seed the watcher persona on a fresh database. Nothing needs rebuilding
        # here any more: the AVAILABLE COMMANDS block is derived from the live
        # registry each time the persona is read, and the authored halves live
        # in Mongo — so neither a redeploy nor a newly added tool can leave the
        # model reading a stale command list.
        try:
            import falcon.watcher_persona as Persona
            Persona.get_parts()
            logger.info(
                "Watcher persona ready (%d chars assembled)", len(Persona.assemble())
            )
        except Exception as persona_exc:
            logger.warning("Watcher persona not initialised: %s", persona_exc)

        # Start watcher threads for any identities with watcher_enabled=True.
        try:
            from falcon.watcher import bootstrap_watchers
            bootstrap_watchers()
        except Exception as watcher_exc:
            logger.warning("Watcher bootstrap skipped: %s", watcher_exc)

        # Start the research worker. Jobs left unfinished by a previous process
        # are resumed automatically — their heartbeat has gone stale, which is
        # what makes them claimable again.
        try:
            from falcon.research import bootstrap as bootstrap_research
            bootstrap_research()
        except Exception as research_exc:
            logger.warning("Research worker not started: %s", research_exc)

        # Periodic snapshot of the live database into a sibling database on the
        # same cluster (falcon → falcon_backup). Due-ness is read from Mongo, so
        # restarts and redeploys neither reset the schedule nor re-run it.
        try:
            from falcon.backup import start_scheduler as start_backup_scheduler
            start_backup_scheduler()
        except Exception as backup_exc:
            logger.warning("Backup scheduler not started: %s", backup_exc)

        # Lumen Guard — the health monitor. Started last, after the subsystems
        # it checks, so its first run sees a settled system rather than
        # reporting half of startup as down. Never fatal: a system running
        # unmonitored is bad, a system that will not boot is worse.
        try:
            from falcon.lumen_guard import start_monitor

            start_monitor()
        except Exception as lumen_exc:
            logger.warning("Lumen Guard not started: %s", lumen_exc)

    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB warmup skipped (will retry lazily): %s", exc)
    yield
    # Graceful shutdown: stop all watchers, then close the shared MongoClient.
    try:
        from falcon.watcher import stop_all_watchers, stop_result_broadcaster
        stop_all_watchers()
        stop_result_broadcaster()
    except Exception:  # noqa: BLE001
        pass
    try:
        from falcon.research import stop_worker as stop_research_worker
        stop_research_worker()
    except Exception:  # noqa: BLE001
        pass
    try:
        from falcon.backup import stop_scheduler as stop_backup_scheduler
        stop_backup_scheduler()
    except Exception:  # noqa: BLE001
        pass
    try:
        from falcon.lumen_guard import stop_monitor
        stop_monitor()
    except Exception:  # noqa: BLE001
        pass
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
    async def debug_env(_: dict = Depends(require_admin)):
        """Which secrets are present. Admin only, and names are never listed.

        This was open to anyone and returned ``all_keys`` — every environment
        variable name in the process. Masked values do not make that safe: the
        names alone map the deployment, and the endpoint's whole purpose is to
        confirm a misconfiguration, which is exactly what an attacker wants too.
        """
        import os
        import falcon.config as Config

        def _mask(v: str) -> str:
            return f"set (…{v[-4:]})" if v and v.strip() else "NOT SET"

        return {
            "OPENROUTER_API_KEY": _mask(os.environ.get("OPENROUTER_API_KEY", "")),
            "OPENAI_API_KEY": _mask(os.environ.get("OPENAI_API_KEY", "")),
            "MONGODB_URI": _mask(os.environ.get("MONGODB_URI", "")),
            "SECRET_KEY": _mask(os.environ.get("SECRET_KEY", "")),
            "ELEVENLABS_API_KEY": _mask(os.environ.get("ELEVENLABS_API_KEY", "")),
            # Resolved background-task routing — this is what actually decides
            # whether summary + memory extraction hit OpenAI or OpenRouter.
            "background_use_openai": Config.background_use_openai,
            "openai_background_model": Config.openai_background_model,
        }

    # ── Routers ────────────────────────────────────────────────────────────
    from app.routers import (
        admin,
        audit,
        categories as categories_router,
        chat,
        config as config_router,
        documents,
        dual_run,
        identities,
        lumen,
        memory,
        testing,
        traces,
        voice,
        watcher as watcher_router,
    )

    prefix = settings.api_prefix

    # Authentication is applied at the mount, not inside the handlers.
    #
    # Every router below except `admin` carries `Depends(require_user)` for the
    # whole router, so a new route is protected the moment it is added and there
    # is no per-handler decision to forget. That is the failure this replaced:
    # auth used to be opt-in per route, and fifty-five routes across twelve
    # routers had never opted in — the entire conversation, memory, audit,
    # document and inference surface answered to any caller that could reach the
    # port.
    #
    # `admin` is mounted bare because it owns /admin/login, which by definition
    # cannot require a token. Its other routes declare their own dependency.
    # `watcher` and `lumen` likewise already gate each route, several of them at
    # admin level; the blanket dependency is added anyway so that neither an
    # unguarded route nor a future one can slip through.
    protected = [Depends(require_user)]

    app.include_router(admin.router, prefix=prefix)
    app.include_router(config_router.router, prefix=prefix, dependencies=protected)
    app.include_router(identities.router, prefix=prefix, dependencies=protected)
    app.include_router(chat.router, prefix=prefix, dependencies=protected)
    app.include_router(memory.router, prefix=prefix, dependencies=protected)
    app.include_router(traces.router, prefix=prefix, dependencies=protected)
    app.include_router(audit.router, prefix=prefix, dependencies=protected)
    app.include_router(dual_run.router, prefix=prefix, dependencies=protected)
    app.include_router(testing.router, prefix=prefix, dependencies=protected)
    app.include_router(voice.router, prefix=prefix, dependencies=protected)
    app.include_router(documents.router, prefix=prefix, dependencies=protected)
    app.include_router(categories_router.router, prefix=prefix, dependencies=protected)
    app.include_router(watcher_router.router, prefix=prefix, dependencies=protected)
    # Mounted bare: EventSource cannot send an Authorization header, so this one
    # route reads its token from the query string and checks it itself.
    app.include_router(watcher_router.stream_router, prefix=prefix)
    app.include_router(lumen.router, prefix=prefix, dependencies=protected)

    return app


app = create_app()
