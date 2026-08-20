"""
watcher.py — Conversation Watcher-Agent for Falcon.

Monitors the MongoDB messages collection for new assistant messages
that contain [AGENT: <command>] ... [/AGENT] markers.
On detection, parses the command + payload, executes via the tool
registry, and injects the result back as a visually distinct
assistant message with _watcher=True.

Marker format (closing tag required):
    [AGENT: command]
    optional payload text
    [/AGENT]

    or on a single line with no payload:
    [AGENT: ping][/AGENT]

    Legacy open-only format (no closing tag) is still supported as
    a fallback so old prompts keep working — the payload runs to the
    next marker or end of message, same as before.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from falcon.db import get_db

logger = logging.getLogger("falcon.watcher")

POLL_INTERVAL_S: float = 1.0

_RESULT_OPEN  = "[AGENT RESULT]"
_RESULT_CLOSE = "[/AGENT RESULT]"

# ---------------------------------------------------------------------------
# Push bus — per-identity SSE subscriber queues
# ---------------------------------------------------------------------------
# Maps identity_id → set of asyncio.Queue instances.
# Each connected SSE client gets its own queue; the watcher thread pushes
# a message dict into every queue for that identity when a result is ready.
# Queues are registered/deregistered by the SSE endpoint.

_push_bus: dict[str, set[asyncio.Queue]] = {}
_push_lock = threading.Lock()


def _register_queue(identity_id: str, q: asyncio.Queue) -> None:
    with _push_lock:
        _push_bus.setdefault(identity_id, set()).add(q)


def _deregister_queue(identity_id: str, q: asyncio.Queue) -> None:
    with _push_lock:
        bucket = _push_bus.get(identity_id)
        if bucket:
            bucket.discard(q)


def _push_to_subscribers(identity_id: str, message: dict) -> None:
    """Push a message dict to every SSE subscriber for identity_id.
    Called from the watcher thread — uses thread-safe loop.call_soon_threadsafe.
    """
    with _push_lock:
        queues = list(_push_bus.get(identity_id, set()))
    for q in queues:
        loop = getattr(q, "_loop", None)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(q.put_nowait, message)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------

# Opening tag: [AGENT: command] or [ACTION: command]
_OPEN_RE = re.compile(
    r"\[(?:AGENT|ACTION)\s*:\s*([^\]]+)\]",
    re.IGNORECASE,
)
# Closing tag: [/AGENT] or [/ACTION]
_CLOSE_RE = re.compile(
    r"\[/(?:AGENT|ACTION)\]",
    re.IGNORECASE,
)


def parse_markers(text: str) -> list[dict]:
    """Extract all [AGENT: cmd]...[/AGENT] blocks from text.

    Preferred format (explicit closing tag):
        [AGENT: echo]
        hello world
        [/AGENT]

    Fallback (no closing tag — legacy / single-line):
        [AGENT: ping]
        (payload runs to the next opening tag or end of string)

    Returns list of {command: str, payload: str} dicts in order.
    """
    results: list[dict] = []
    remaining = text
    pos = 0

    while pos < len(remaining):
        open_m = _OPEN_RE.search(remaining, pos)
        if not open_m:
            break

        command = open_m.group(1).strip()
        after_open = open_m.end()

        # Look for a closing [/AGENT] tag after the opening tag
        close_m = _CLOSE_RE.search(remaining, after_open)

        # Also look for the next opening tag (for fallback boundary)
        next_open_m = _OPEN_RE.search(remaining, after_open)

        if close_m and (next_open_m is None or close_m.start() < next_open_m.start()):
            # Preferred: explicit closing tag found before the next opener
            payload = remaining[after_open:close_m.start()].strip()
            pos = close_m.end()
        else:
            # Fallback: no closing tag — payload runs to next opener or end
            payload_end = next_open_m.start() if next_open_m else len(remaining)
            payload = remaining[after_open:payload_end].strip()
            pos = payload_end

        results.append({"command": command, "payload": payload})

    return results


def format_result(result_text: str) -> str:
    """Wrap a tool result in the standard [AGENT RESULT] block."""
    return f"{_RESULT_OPEN}\n{result_text.strip()}\n{_RESULT_CLOSE}"


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def _mark_processed(msg_id: ObjectId, identity_id: str) -> None:
    """Record that this message has been processed (idempotency guard)."""
    db = get_db()
    db["watcher_processed"].update_one(
        {"msg_id": msg_id},
        {"$setOnInsert": {
            "msg_id": msg_id,
            "identity_id": identity_id,
            "processed_at": _utc_now_iso(),
        }},
        upsert=True,
    )


def _is_processed(msg_id: ObjectId) -> bool:
    db = get_db()
    return db["watcher_processed"].find_one({"msg_id": msg_id}) is not None


# ---------------------------------------------------------------------------
# Invocation logger
# ---------------------------------------------------------------------------

def _log_invocation(
    identity_id: str,
    msg_id: str,
    command: str,
    payload: str,
    result: str,
    latency_ms: int,
    error: bool,
) -> None:
    db = get_db()
    db["watcher_log"].insert_one({
        "identity_id": identity_id,
        "msg_id": msg_id,
        "command": command,
        "payload": payload[:500],
        "result": result[:2000],
        "latency_ms": latency_ms,
        "error": error,
        "recorded_at": _utc_now_iso(),
    })


# ---------------------------------------------------------------------------
# Result injection
# ---------------------------------------------------------------------------

def _inject_result(identity_id: str, result_text: str) -> None:
    """Insert the watcher result as a distinct assistant message, then push to SSE subscribers."""
    db = get_db()
    ts = _utc_now_iso()
    doc = {
        "identity_id": identity_id,
        "timestamp": ts,
        "role": "assistant",
        "content": format_result(result_text),
        "_watcher": True,
    }
    db["messages"].insert_one(doc)

    # Push immediately to every connected SSE client — no polling needed.
    _push_to_subscribers(identity_id, {
        "type": "watcher_result",
        "timestamp": ts,
        "content": format_result(result_text),
    })


# ---------------------------------------------------------------------------
# Core scan + execute loop
# ---------------------------------------------------------------------------

def _process_message(identity_id: str, msg_doc: dict) -> None:
    """Scan one assistant message, execute any markers, inject results."""
    from falcon.watcher_tools import dispatch

    content = msg_doc.get("content", "") or ""
    msg_id = msg_doc["_id"]

    # Guard: only assistant messages, not watcher result messages themselves
    if msg_doc.get("role") != "assistant":
        return
    if msg_doc.get("_watcher"):
        return  # skip our own injected results

    markers = parse_markers(content)
    if not markers:
        return

    logger.info(
        "watcher: found %d marker(s) in msg %s for identity=%r",
        len(markers), msg_id, identity_id,
    )

    # Mark processed BEFORE execution so a crash mid-run doesn't cause a retry.
    _mark_processed(msg_id, identity_id)

    for m in markers:
        command = m["command"]
        payload = m["payload"]
        t0 = time.monotonic()
        result = dispatch(command, payload)
        latency_ms = round((time.monotonic() - t0) * 1000)
        error = result.startswith("[ERROR]") or result.startswith("[NOT CONFIGURED]")

        logger.info(
            "watcher: executed command=%r latency=%dms error=%s for identity=%r",
            command, latency_ms, error, identity_id,
        )

        _log_invocation(
            identity_id=identity_id,
            msg_id=str(msg_id),
            command=command,
            payload=payload,
            result=result,
            latency_ms=latency_ms,
            error=error,
        )
        _inject_result(identity_id, result)


# ---------------------------------------------------------------------------
# WatcherThread — one per identity
# ---------------------------------------------------------------------------

class WatcherThread(threading.Thread):
    """Background daemon thread that polls for new assistant messages."""

    def __init__(self, identity_id: str):
        super().__init__(
            name=f"falcon-watcher-{identity_id}",
            daemon=True,
        )
        self.identity_id = identity_id
        self._stop_event = threading.Event()
        # Cursor: the _id of the last message we have seen. We start from the
        # most recent existing message so we don't reprocess history on startup.
        self._cursor_id: Optional[ObjectId] = None

    def _init_cursor(self) -> None:
        """Set cursor to the newest existing message for this identity."""
        db = get_db()
        latest = db["messages"].find_one(
            {"identity_id": self.identity_id},
            sort=[("_id", -1)],
        )
        if latest:
            self._cursor_id = latest["_id"]
        # If no messages yet, cursor stays None and we start from the beginning.

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("watcher: started for identity=%r", self.identity_id)
        try:
            self._init_cursor()
        except Exception as exc:  # noqa: BLE001
            logger.error("watcher: cursor init failed for identity=%r: %s", self.identity_id, exc)

        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as exc:  # noqa: BLE001
                logger.error("watcher: poll error for identity=%r: %s", self.identity_id, exc)
            self._stop_event.wait(POLL_INTERVAL_S)

        logger.info("watcher: stopped for identity=%r", self.identity_id)

    def _poll(self) -> None:
        db = get_db()
        query: dict = {
            "identity_id": self.identity_id,
            "role": "assistant",
        }
        if self._cursor_id is not None:
            query["_id"] = {"$gt": self._cursor_id}

        new_msgs = list(
            db["messages"]
            .find(query)
            .sort("_id", 1)
            .limit(50)
        )

        for msg in new_msgs:
            # Always advance the cursor even if we skip the message.
            self._cursor_id = msg["_id"]

            if _is_processed(msg["_id"]):
                continue

            try:
                _process_message(self.identity_id, msg)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "watcher: _process_message raised for identity=%r msg=%s: %s",
                    self.identity_id, msg["_id"], exc,
                )
                # Mark processed even on error so we don't keep retrying.
                _mark_processed(msg["_id"], self.identity_id)


# ---------------------------------------------------------------------------
# Service registry — one thread per identity
# ---------------------------------------------------------------------------

_watchers: dict[str, WatcherThread] = {}
_lock = threading.Lock()


def start_watcher(identity_id: str) -> None:
    """Start a watcher for identity_id. No-op if already running."""
    with _lock:
        existing = _watchers.get(identity_id)
        if existing and existing.is_alive():
            return
        t = WatcherThread(identity_id)
        t.start()
        _watchers[identity_id] = t
        logger.info("watcher: registered for identity=%r", identity_id)


def stop_watcher(identity_id: str) -> None:
    """Gracefully stop the watcher for identity_id."""
    with _lock:
        t = _watchers.pop(identity_id, None)
    if t:
        t.stop()
        logger.info("watcher: stop requested for identity=%r", identity_id)


def stop_all_watchers() -> None:
    """Stop all running watchers. Called on FastAPI shutdown."""
    with _lock:
        ids = list(_watchers.keys())
    for iid in ids:
        stop_watcher(iid)


def is_running(identity_id: str) -> bool:
    with _lock:
        t = _watchers.get(identity_id)
    return t is not None and t.is_alive()


# ---------------------------------------------------------------------------
# Bootstrap helpers — called from main.py lifespan
# ---------------------------------------------------------------------------

def get_enabled_identities() -> list[str]:
    """Return identity_ids of all users (portal + admin + watcher_settings) with watcher_enabled=True."""
    try:
        db = get_db()
        ids: list[str] = []

        for d in db["portal_users"].find(
            {"watcher_enabled": True, "disabled": {"$ne": True}},
            {"_id": 0, "identity_id": 1},
        ):
            if d.get("identity_id"):
                ids.append(d["identity_id"])

        for d in db["admin_users"].find(
            {"watcher_enabled": True, "disabled": {"$ne": True}},
            {"_id": 0, "identity_id": 1},
        ):
            iid = d.get("identity_id")
            if iid and iid not in ids:
                ids.append(iid)

        # watcher_settings: fallback store for identities not in either user collection
        for d in db["watcher_settings"].find(
            {"watcher_enabled": True},
            {"_id": 0, "identity_id": 1},
        ):
            iid = d.get("identity_id")
            if iid and iid not in ids:
                ids.append(iid)

        return ids
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher: get_enabled_identities failed: %s", exc)
        return []


def bootstrap_watchers() -> None:
    """Start watchers for all identities that have watcher_enabled=True.
    Called once during FastAPI lifespan startup.
    """
    ids = get_enabled_identities()
    for iid in ids:
        start_watcher(iid)
    if ids:
        logger.info("watcher: bootstrapped %d watcher(s): %s", len(ids), ids)
    else:
        logger.info("watcher: no identities with watcher_enabled=True at startup")
