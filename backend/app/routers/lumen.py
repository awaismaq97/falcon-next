"""
lumen.py — Lumen Guard API. Admin only, every route.

Lumen Guard checks whether each part of the running system is working: the
database, auth, the watcher, the tool registry, the persona, storage and the
background workers. See falcon/lumen_guard.py for what each check actually does.

Everything here is behind ``require_admin``. The results name identities, admin
account counts and failed-login activity, so there is deliberately no
portal-user variant and no unauthenticated health endpoint.

Routes (admin only):
  GET    /admin/lumen/status   — monitor state + the last stored result
  POST   /admin/lumen/check    — run every check now and return the report
  POST   /admin/lumen/start    — start the background monitor
  POST   /admin/lumen/stop     — stop the background monitor

Nothing here changes the system being checked. The database check writes and
reads back a single probe document in Lumen's own collection, which is what
proves storage works; nothing else is touched.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

import falcon.admin_audit as AdminAudit
import falcon.lumen_guard as Lumen
from app.routers.admin import require_admin

router = APIRouter(tags=["lumen"])


@router.get("/admin/lumen/status")
def lumen_status(payload: dict = Depends(require_admin)) -> dict[str, Any]:
    """Monitor state and the most recent result, without re-running anything."""
    return Lumen.status()


@router.post("/admin/lumen/check")
def lumen_check(payload: dict = Depends(require_admin)) -> dict[str, Any]:
    """Run every check now."""
    report = Lumen.run_checks()
    broken = [c["name"] for c in report["checks"] if c["state"] != Lumen.OK]
    AdminAudit.log_admin_action(
        actor=payload["username"],
        action="lumen_check",
        details={"overall": report["overall"], "not_ok": broken},
    )
    return report


@router.post("/admin/lumen/start")
def lumen_start(payload: dict = Depends(require_admin)) -> dict[str, Any]:
    Lumen.start_monitor()
    AdminAudit.log_admin_action(actor=payload["username"], action="lumen_start")
    return Lumen.status()


@router.post("/admin/lumen/stop")
def lumen_stop(payload: dict = Depends(require_admin)) -> dict[str, Any]:
    """Stop the monitor in this process. Logged, so it cannot be turned off quietly."""
    Lumen.stop_monitor()
    AdminAudit.log_admin_action(actor=payload["username"], action="lumen_stop")
    return Lumen.status()
