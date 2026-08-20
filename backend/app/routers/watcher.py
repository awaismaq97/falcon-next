"""
watcher.py router — Watcher-Agent management API.

Routes:
  GET    /watcher/status                    — list all running watcher identities
  GET    /watcher/tools                     — list registered tool names
  GET    /watcher/log                       — global invocation log (admin)
  GET    /identities/{id}/watcher/status    — is watcher running for this identity?
  GET    /identities/{id}/watcher/stream    — SSE push stream (instant result delivery)
  GET    /identities/{id}/watcher/log       — invocation log for one identity
  DELETE /identities/{id}/watcher/log       — clear log for one identity

  Admin-only (require_admin from admin router):
  POST   /admin/users/{user_id}/watcher     — enable/disable watcher for a portal user
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import falcon.watcher as Watcher
import falcon.watcher_tools as WatcherTools
import falcon.admin_users as AdminUsers
from falcon.admin_auth import decode_access_token
from falcon.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["watcher"])
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth helpers (mirrors admin.py — no circular import needed)
# ---------------------------------------------------------------------------

def _decode(creds: HTTPAuthorizationCredentials) -> dict:
    try:
        return decode_access_token(creds.credentials)
    except JWTError:
        raise HTTPException(401, "Invalid or expired authentication token.")


def _require_any(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(401, "Authentication required.")
    return _decode(creds)


def _require_admin(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(401, "Authentication required.")
    payload = _decode(creds)
    if payload.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return payload


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class WatcherToggleRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Status + tools
# ---------------------------------------------------------------------------

@router.get("/watcher/status")
def watcher_global_status(_: dict = Depends(_require_admin)) -> dict:
    """List every identity that currently has a running watcher."""
    from falcon.watcher import _watchers, _lock
    with _lock:
        running = [iid for iid, t in _watchers.items() if t.is_alive()]
    return {"running": running, "count": len(running)}


@router.get("/watcher/tools")
def list_tools(_: dict = Depends(_require_any)) -> dict:
    """Return the names of all registered watcher tools."""
    return {"tools": WatcherTools.list_tools()}


@router.get("/watcher/log")
def global_watcher_log(
    limit: int = Query(100, ge=1, le=500),
    _: dict = Depends(_require_admin),
) -> dict:
    """Return the most recent watcher invocations across all identities."""
    db = get_db()
    cursor = (
        db["watcher_log"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    records = list(cursor)
    return {"records": records, "count": len(records)}


# ---------------------------------------------------------------------------
# Per-identity
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/watcher/status")
def identity_watcher_status(
    identity_id: str,
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Is the watcher currently running for this identity?

    Returns a safe response even when unauthenticated (running=False, enabled=False)
    so the frontend polling doesn't produce a cascade of 401 errors when the
    token is missing or expired.
    """
    # Unauthenticated — return safe defaults instead of 401 so the chat tab
    # polling doesn't spam the logs when the JWT hasn't loaded yet.
    if not creds:
        return {"identity_id": identity_id, "running": False, "enabled": False}

    try:
        auth = _decode(creds)
    except Exception:
        return {"identity_id": identity_id, "running": False, "enabled": False}

    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    running = Watcher.is_running(identity_id)
    db = get_db()
    # Check portal_users first, then admin_users, then watcher_settings fallback
    user_doc = db["portal_users"].find_one({"identity_id": identity_id}, {"watcher_enabled": 1})
    if not user_doc:
        user_doc = db["admin_users"].find_one({"identity_id": identity_id}, {"watcher_enabled": 1})
    if not user_doc:
        user_doc = db["watcher_settings"].find_one({"identity_id": identity_id}, {"watcher_enabled": 1})
    enabled = bool((user_doc or {}).get("watcher_enabled", False))

    return {
        "identity_id": identity_id,
        "running": running,
        "enabled": enabled,
    }


@router.get("/identities/{identity_id}/watcher/stream")
async def watcher_stream(
    identity_id: str,
    token: str | None = Query(None),
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
):
    """SSE stream that pushes watcher results to the client instantly.

    The client connects once and keeps the connection open. Whenever the
    watcher injects a [AGENT RESULT] message it is pushed here immediately —
    no polling delay. The frontend appends it directly to the chat cache.

    Auth token accepted via Authorization header OR ?token= query param
    (EventSource API does not support custom headers).

    Sends a keepalive ping every 15 seconds to prevent proxy timeouts.
    """
    import json as _json

    # Resolve token: header takes priority, fall back to query param
    raw_token = (creds.credentials if creds else None) or token
    if raw_token:
        try:
            auth = decode_access_token(raw_token)
            if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
                raise HTTPException(403, "Access denied.")
        except HTTPException:
            raise
        except Exception:
            pass  # invalid token — allow through, stream safe defaults

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    q._loop = loop  # type: ignore[attr-defined]

    Watcher._register_queue(identity_id, q)

    async def event_generator():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {
                        "event": msg.get("type", "watcher_result"),
                        "data": _json.dumps(msg),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        except asyncio.CancelledError:
            pass
        finally:
            Watcher._deregister_queue(identity_id, q)

    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )


@router.get("/identities/{identity_id}/watcher/log")
def identity_watcher_log(
    identity_id: str,
    limit: int = Query(50, ge=1, le=200),
    auth: dict = Depends(_require_any),
) -> dict:
    """Return watcher invocation log for one identity."""
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")
    db = get_db()
    cursor = (
        db["watcher_log"]
        .find({"identity_id": identity_id}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    records = list(cursor)
    return {"records": records, "count": len(records)}


@router.delete("/identities/{identity_id}/watcher/log")
def clear_watcher_log(
    identity_id: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """Clear the watcher log for one identity."""
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")
    db = get_db()
    result = db["watcher_log"].delete_many({"identity_id": identity_id})
    return {"deleted_count": result.deleted_count}


# ---------------------------------------------------------------------------
# Admin: enable/disable watcher per portal user
# ---------------------------------------------------------------------------

@router.post("/admin/users/{user_id}/watcher")
def set_watcher_enabled(
    user_id: str,
    req: WatcherToggleRequest,
    _: dict = Depends(_require_admin),
) -> dict:
    """Enable or disable the watcher for a portal user (admin only).

    When enabled, starts the watcher thread for the user's identity_id.
    When disabled, stops it.
    """
    user = AdminUsers.get_portal_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found.")

    identity_id = user.get("identity_id", "")
    if not identity_id:
        raise HTTPException(400, "User has no associated identity_id.")

    from bson import ObjectId
    db = get_db()
    db["portal_users"].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"watcher_enabled": req.enabled}},
    )

    if req.enabled:
        Watcher.start_watcher(identity_id)
    else:
        Watcher.stop_watcher(identity_id)

    return {
        "user_id": user_id,
        "identity_id": identity_id,
        "watcher_enabled": req.enabled,
        "running": Watcher.is_running(identity_id),
    }


@router.post("/identities/{identity_id}/watcher/toggle")
def toggle_watcher_for_identity(
    identity_id: str,
    req: WatcherToggleRequest,
    auth: dict = Depends(_require_admin),
) -> dict:
    """Enable or disable the watcher directly for any identity_id (admin only).

    Works for both admin and portal user identities. Stores the flag in
    watcher_settings (upsert) which is the single authoritative store for
    this endpoint — no dependency on which user collection holds the identity.
    """
    db = get_db()

    # watcher_settings is the guaranteed store — always upsert here.
    db["watcher_settings"].update_one(
        {"identity_id": identity_id},
        {"$set": {"identity_id": identity_id, "watcher_enabled": req.enabled}},
        upsert=True,
    )

    # Also mirror to the correct user collection so bootstrap reads are consistent.
    db["portal_users"].update_many(
        {"identity_id": identity_id},
        {"$set": {"watcher_enabled": req.enabled}},
    )
    db["admin_users"].update_many(
        {"identity_id": identity_id},
        {"$set": {"watcher_enabled": req.enabled}},
    )

    if req.enabled:
        Watcher.start_watcher(identity_id)
        logger.info("watcher: manually started for identity=%r by admin=%r", identity_id, auth.get("username"))
    else:
        Watcher.stop_watcher(identity_id)
        logger.info("watcher: manually stopped for identity=%r by admin=%r", identity_id, auth.get("username"))

    return {
        "identity_id": identity_id,
        "watcher_enabled": req.enabled,
        "running": Watcher.is_running(identity_id),
    }
