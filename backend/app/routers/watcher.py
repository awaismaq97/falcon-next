"""
watcher.py router — Watcher-Agent management API.

Routes:
  GET    /watcher/status                    — list all running watcher identities
  GET    /watcher/tools                     — list registered tool names
  GET    /watcher/persona                   — persona parts + derived commands (admin)
  PUT    /watcher/persona                   — edit the authored halves (admin)
  POST   /watcher/persona/reset             — restore shipped defaults (admin)
  GET    /watcher/agents                    — tools annotated builtin/generated
  POST   /watcher/agents                    — create an agent (name + purpose)
  POST   /watcher/agents/{name}/run         — run one tool directly (agents feature)
  DELETE /watcher/agents/{name}             — delete a spawned agent
  GET    /watcher/debug                     — live vs persisted tool registry (admin)
  GET    /watcher/log                       — global invocation log (admin)
  GET    /identities/{id}/watcher/status    — is watcher running for this identity?
  GET    /identities/{id}/watcher/stream    — SSE push stream (instant result delivery)
  GET    /identities/{id}/watcher/log       — invocation log for one identity
  DELETE /identities/{id}/watcher/log       — clear log for one identity
  GET    /identities/{id}/research          — research jobs (summaries)
  GET    /identities/{id}/research/{job_id} — one job, with findings + report
  POST   /identities/{id}/research/{job_id}/cancel — stop a running job
  DELETE /identities/{id}/research/{job_id} — permanently delete a job

  Admin-only (require_admin from admin router):
  POST   /admin/users/{user_id}/watcher     — enable/disable watcher for a portal user
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

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


def _require_agents_feature(auth: dict = Depends(_require_any)) -> dict:
    """Allow only accounts the admin has given the ``agents`` feature.

    Read from the database on every call rather than from the JWT: features are
    not in the token, and a token issued before the admin revoked the feature
    would otherwise keep working for its full lifetime. Hiding the tab in the
    frontend is presentation; this is the part that actually stops a run.
    """
    if auth.get("role") == "admin":
        return auth

    user = AdminUsers.get_portal_user_by_id(str(auth.get("sub") or ""))
    if not user or not (user.get("features") or {}).get("agents", False):
        raise HTTPException(
            403,
            "Running agents directly is not enabled for this account. "
            "Ask an administrator to turn on the Watcher Agents feature.",
        )
    return auth


def _resolve_identity(auth: dict, requested: str) -> str:
    """Whose data a manual run acts on.

    Portal users are pinned to their own identity — the tools reach per-identity
    storage, so an unchecked identity_id in the body would be a way to read and
    delete another user's documents.
    """
    own = (auth.get("identity_id") or "default").strip()
    asked = (requested or "").strip()
    if auth.get("role") == "admin":
        return asked or own
    if asked and asked != own:
        raise HTTPException(403, "You can only run agents against your own identity.")
    return own


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class WatcherToggleRequest(BaseModel):
    enabled: bool


class PersonaUpdateRequest(BaseModel):
    """The authored halves of the watcher persona.

    The AVAILABLE COMMANDS block is deliberately absent — it is derived from the
    live tool registry on every read, so it is not editable and cannot drift.
    """
    preamble: str
    rules: str = ""


class TweetTextRequest(BaseModel):
    """Body for confirming or editing a staged tweet.

    On confirm, ``text`` is whatever the user had on screen when they pressed
    Post — sent with the click so what they saw is exactly what publishes.
    Omitted, the stored draft is used unchanged.
    """
    text: str | None = None


class AgentCreateRequest(BaseModel):
    """Body for POST /watcher/agents — the UI's "add agent" form.

    ``name`` is optional: left blank, the name is derived from ``purpose`` by
    the same model call the chat path uses, so the form works with only a
    description.
    """
    purpose: str
    name: str = ""


class AgentRunRequest(BaseModel):
    """Body for POST /watcher/agents/{name}/run — the Watcher Agents tab.

    ``identity_id`` is honoured only for admins; a portal user is pinned to
    their own identity regardless of what they send. See ``_resolve_identity``.
    """
    payload: str = ""
    identity_id: str = ""


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


@router.get("/watcher/debug")
def watcher_debug(_: dict = Depends(_require_admin)) -> dict:
    """Debug: show which tools are live in this process vs persisted in Mongo."""
    import falcon.watcher_generated as Generated

    stored = Generated.list_all()
    live = WatcherTools.list_tools()
    return {
        "pid": os.getpid(),
        "tools": live,
        "generated_stored": [
            {"name": d["name"], "revision": d.get("revision"), "created_at": d.get("created_at")}
            for d in stored
        ],
        # Non-empty means this process has drifted from the store; dispatch will
        # heal each entry on first use, but it signals a startup-load failure.
        "generated_missing_from_registry": [d["name"] for d in stored if d["name"] not in live],
    }


@router.get("/watcher/persona")
def get_persona(_: dict = Depends(_require_admin)) -> dict:
    """The watcher persona: both authored halves, the derived command list, and
    the assembled result the model actually receives."""
    import falcon.watcher_persona as Persona

    return Persona.describe()


@router.put("/watcher/persona")
def update_persona(body: PersonaUpdateRequest, auth: dict = Depends(_require_admin)) -> dict:
    """Rewrite the authored halves of the persona.

    Admin-only: the watcher persona is global, shared by every watcher-enabled
    identity, so an edit here changes behaviour for all of them.
    """
    import falcon.watcher_persona as Persona

    try:
        Persona.save_parts(body.preamble, body.rules, updated_by=auth.get("username") or "admin")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return Persona.describe()


@router.post("/watcher/persona/reset")
def reset_persona(auth: dict = Depends(_require_admin)) -> dict:
    """Restore the shipped defaults."""
    import falcon.watcher_persona as Persona

    Persona.reset_parts(updated_by=auth.get("username") or "admin")
    return Persona.describe()


@router.get("/watcher/agents")
def list_agents(auth: dict = Depends(_require_any)) -> dict:
    """Every registered tool, annotated so the UI can show, run and manage them.

    ``kind`` is "generated" for tools spawned at runtime (they live in the
    ``watcher_generated_tools`` collection and carry their source) and "builtin"
    for those defined in watcher_tools.py. Only generated tools are deletable —
    a built-in would simply reappear on the next process start.

    ``use_when``, ``payload_hint`` and ``example`` come from the same map that
    describes each tool to the model, rather than from a second set of texts
    written for the UI. One source means the person pressing Run and the model
    emitting a command are working from the same description of what the tool
    takes — and a tool spawned at runtime gets a description here for free.
    """
    import falcon.watcher_generated as Generated
    import falcon.watcher_persona as Persona

    stored = {d["name"]: d for d in Generated.list_all()}
    agents = []
    for name in WatcherTools.list_tools():
        doc = stored.get(name)
        handler = WatcherTools._REGISTRY.get(name)
        # Built-ins document themselves in their docstring; generated tools
        # carry the spawn prompt that produced them.
        summary = (doc or {}).get("context") or ""
        if not summary and handler and handler.__doc__:
            summary = handler.__doc__.strip().split("\n", 1)[0]

        described = Persona.BUILTIN_DESCRIPTIONS.get(name, {})
        agents.append({
            "name": name,
            "kind": "generated" if doc else "builtin",
            "deletable": bool(doc) and name not in WatcherTools.PROTECTED_TOOLS,
            "summary": summary,
            "code": (doc or {}).get("code"),
            "revision": (doc or {}).get("revision"),
            "created_at": (doc or {}).get("created_at"),
            "use_when": described.get("use_when", ""),
            "payload_hint": described.get("payload", ""),
            "example": described.get("example", ""),
            "destructive": name in WatcherTools.DESTRUCTIVE_TOOLS,
        })

    return {"agents": agents, "count": len(agents)}


@router.post("/watcher/agents/{name}/run")
def run_agent(
    name: str,
    body: AgentRunRequest,
    auth: dict = Depends(_require_agents_feature),
) -> dict:
    """Run one tool directly and return what it returned.

    The same dispatch the watcher performs, minus the model: no marker is
    parsed, no message is scanned, and the result is handed back to the caller
    instead of being injected into the conversation. That makes this the way to
    use a tool deliberately — and the way to find out whether a tool works
    without having to talk the assistant into emitting a command.

    Runs in FastAPI's threadpool (a plain ``def``) because dispatch blocks: a
    fetch, a database round-trip or a model call inside a tool would otherwise
    stall the event loop for every other request.
    """
    key = name.lower().strip()
    if key not in WatcherTools.list_tools():
        raise HTTPException(404, f"No agent named '{key}'.")

    identity_id = _resolve_identity(auth, body.identity_id)
    payload = body.payload or ""
    actor = auth.get("username") or identity_id

    # Tools that own per-identity state read this, exactly as they do under the
    # watcher. The run id stands in for the message id a dispatched command
    # would carry, so a staged tweet records where it came from and the log row
    # can be told apart from one the watcher produced.
    run_id = f"manual-{uuid4().hex[:12]}"
    WatcherTools.set_current_identity(identity_id, run_id)
    t0 = time.monotonic()
    try:
        result = WatcherTools.dispatch(key, payload)
    finally:
        # Threadpool workers are reused, so leaving the identity set would hand
        # the next request whatever this one was acting for.
        WatcherTools.set_current_identity("", "")

    latency_ms = round((time.monotonic() - t0) * 1000)
    error = result.startswith("[ERROR]") or result.startswith("[NOT CONFIGURED]")

    # Written to the same log the watcher writes to, so the Logs tab shows every
    # tool run in one place. ``source`` is what says a human pressed Run.
    try:
        get_db()["watcher_log"].insert_one({
            "identity_id": identity_id,
            "msg_id": run_id,
            "command": key,
            "payload": payload[:500],
            "result": result[:2000],
            "latency_ms": latency_ms,
            "error": error,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "manual",
            "actor": actor,
        })
    except Exception as log_exc:  # noqa: BLE001
        # The tool has already run; failing the request now would tell the
        # caller nothing happened when something did.
        logger.warning("watcher: could not log manual run of %r: %s", key, log_exc)

    logger.info(
        "watcher: %r run manually by %r for identity=%r latency=%dms error=%s",
        key, actor, identity_id, latency_ms, error,
    )

    return {
        "agent": key,
        "identity_id": identity_id,
        "run_id": run_id,
        "payload": payload,
        "result": result,
        "latency_ms": latency_ms,
        "error": error,
    }


@router.post("/watcher/agents", status_code=201)
def create_agent(body: AgentCreateRequest, auth: dict = Depends(_require_any)) -> dict:
    """Create a new agent from a name and a description of what it should do.

    The REST equivalent of telling the assistant to spawn one. Both call
    ``WatcherTools.spawn_agent``, so an agent created here is indistinguishable
    from one created in chat: same code generation, same compile check, same
    registration, and the same persona rebuild — which is what makes the new
    agent immediately visible to the model rather than only after a restart.

    Runs in FastAPI's threadpool (a plain ``def``), so the two blocking model
    calls inside do not stall the event loop.
    """
    import falcon.watcher_generated as Generated

    purpose = body.purpose.strip()
    if not purpose:
        raise HTTPException(400, "Describe what the agent should do — the purpose cannot be empty.")

    result = WatcherTools.spawn_agent(purpose, name=body.name)

    if result.get("status") != "ok":
        message = result.get("message") or "Agent creation failed."
        # A name clash is the one failure the caller can fix by editing the form
        # and retrying, so it is worth distinguishing from a generation failure.
        raise HTTPException(409 if "already exists" in message else 400, message)

    created = result["agent_id"]
    doc = Generated.get(created) or {}

    logger.info(
        "watcher: agent %r created via API by %r",
        created, auth.get("username") or auth.get("identity_id"),
    )

    # Same shape as a row from GET /watcher/agents, so the UI can drop it
    # straight into its existing list without a second round trip.
    return {
        "agent": {
            "name": created,
            "kind": "generated",
            "deletable": created not in WatcherTools.PROTECTED_TOOLS,
            "summary": doc.get("context") or purpose,
            "code": doc.get("code"),
            "revision": doc.get("revision"),
            "created_at": doc.get("created_at"),
        },
        "message": result.get("message", ""),
    }


@router.delete("/watcher/agents/{name}")
def delete_agent(name: str, auth: dict = Depends(_require_any)) -> dict:
    """Delete a spawned agent: its stored code, its registration, its persona entry.

    Removal has to be all three or the system contradicts itself — a tool left in
    the persona but missing from the registry would be advertised to the model
    and then fail on dispatch.
    """
    import falcon.watcher_generated as Generated

    key = name.lower().strip()

    if key in WatcherTools.PROTECTED_TOOLS:
        raise HTTPException(
            400,
            f"'{key}' is protected and cannot be deleted — it is the only way to create new agents.",
        )

    if Generated.get(key) is None:
        # Distinguish "built-in, so not yours to delete" from "no such tool",
        # because the two need very different responses from the user.
        if key in WatcherTools.list_tools():
            raise HTTPException(400, f"'{key}' is a built-in tool and cannot be deleted.")
        raise HTTPException(404, f"No agent named '{key}'.")

    if not Generated.delete(key):
        raise HTTPException(500, f"Failed to delete '{key}' from the agent store.")

    # Rebuild the persona from what is now registered, so the model stops being
    # told about a tool that no longer exists. Called with no arguments, this
    # regenerates the whole AVAILABLE COMMANDS block from the live registry.
    Watcher.refresh_watcher_persona()

    remaining = WatcherTools.list_tools()
    logger.info(
        "watcher: agent %r deleted by %r — %d tools remain",
        key, auth.get("username") or auth.get("identity_id"), len(remaining),
    )
    return {"deleted": key, "tools": remaining}


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


@router.get("/identities/{identity_id}/tweets/{code}")
def get_staged_tweet(
    identity_id: str,
    code: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """State of one staged tweet — drives the Post / Reject card in chat.

    The card reads status from here rather than from the chat message, so a
    tweet already posted or rejected still shows correctly after a reload.
    """
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    doc = WatcherTools.get_staged_tweet(code, identity_id)
    if not doc:
        raise HTTPException(404, f"No staged tweet '{code}'.")
    # The limit is server-side config, so the editor's counter has to be told it
    # rather than hardcoding 280 and disagreeing on a Premium account.
    doc["max_chars"] = WatcherTools.tweet_char_limit()
    return doc


@router.patch("/identities/{identity_id}/tweets/{code}")
def edit_staged_tweet(
    identity_id: str,
    code: str,
    body: TweetTextRequest,
    auth: dict = Depends(_require_any),
) -> dict:
    """Rewrite a staged tweet before it is posted.

    Lets the user fix the agent's wording and keep the edit, rather than
    rejecting the draft and asking for another one.
    """
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    ok, message = WatcherTools.edit_tweet(code, body.text or "", identity_id)
    if not ok:
        raise HTTPException(400, message)
    return {"message": message}


@router.post("/identities/{identity_id}/tweets/{code}/confirm")
def confirm_staged_tweet(
    identity_id: str,
    code: str,
    body: TweetTextRequest | None = None,
    auth: dict = Depends(_require_any),
) -> dict:
    """Publish a staged tweet. The only path that posts to X.

    Deliberately not exposed as a watcher tool: the model can propose a tweet
    but cannot publish one. Reaching this requires a signed-in human, and that
    click is the authorisation — over text the human may have rewritten.
    """
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    ok, message = WatcherTools.confirm_tweet(
        code, identity_id, text=(body.text if body else None)
    )
    if not ok:
        raise HTTPException(400, message)
    logger.info("post_tweet: %r published by %r", code, auth.get("username") or identity_id)
    return {"posted": True, "message": message}


@router.post("/identities/{identity_id}/tweets/{code}/cancel")
def cancel_staged_tweet(
    identity_id: str,
    code: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """Discard a staged tweet without posting it."""
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    ok, message = WatcherTools.cancel_tweet(code, identity_id)
    if not ok:
        raise HTTPException(400, message)
    return {"posted": False, "message": message}


@router.get("/identities/{identity_id}/research")
def list_research_jobs(
    identity_id: str,
    limit: int = Query(20, ge=1, le=100),
    auth: dict = Depends(_require_any),
) -> dict:
    """Research jobs for one identity, newest first — summaries only.

    Findings and the report are omitted here: a finished job's report can run to
    thousands of words, and the list view only needs enough to render a row.
    """
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    import falcon.research as Research

    jobs = Research.list_jobs(identity_id, limit=limit)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/identities/{identity_id}/research/{job_id}")
def get_research_job(
    identity_id: str,
    job_id: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """One research job in full, including its findings and final report."""
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    import falcon.research as Research

    job = Research.get_job(job_id, identity_id)
    if not job:
        raise HTTPException(404, f"No research job '{job_id}' for this identity.")
    return job


@router.post("/identities/{identity_id}/research/{job_id}/cancel")
def cancel_research_job(
    identity_id: str,
    job_id: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """Stop a queued or running job. Whatever it has gathered so far is kept."""
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    import falcon.research as Research

    if not Research.cancel_job(job_id, identity_id):
        raise HTTPException(400, f"Job '{job_id}' is not running, or does not exist.")
    logger.info("research: job %r cancelled via API by %r", job_id, auth.get("username") or identity_id)
    return {"cancelled": job_id}


@router.delete("/identities/{identity_id}/research/{job_id}")
def delete_research_job(
    identity_id: str,
    job_id: str,
    auth: dict = Depends(_require_any),
) -> dict:
    """Permanently delete one research job, including its findings and report.

    The only way research data leaves the database. Nothing expires it on a
    timer — see the retention note in falcon/research.py.
    """
    if auth.get("role") != "admin" and auth.get("identity_id") != identity_id:
        raise HTTPException(403, "Access denied.")

    import falcon.research as Research

    if not Research.delete_job(job_id, identity_id):
        raise HTTPException(404, f"No research job '{job_id}' for this identity.")
    logger.info("research: job %r deleted via API by %r", job_id, auth.get("username") or identity_id)
    return {"deleted": job_id}


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
