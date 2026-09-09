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
import hashlib
import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from falcon.db import get_db

logger = logging.getLogger("falcon.watcher")

POLL_INTERVAL_S: float = 1.0

# Repeat suppression.
#
# `watcher_processed` stops the same *message* running twice. It does nothing
# about the model emitting the same command again in a new message — and that is
# the common case, because the conversation sent back to the model still
# contains its own past [AGENT ...] markers and the [AGENT RESULT] blocks they
# produced. Seeing them primes it to write another one, whose result is then fed
# back in turn. Left alone that compounds until the chat is mostly agent output.
#
# So an identical (command, payload) runs at most once per cooldown. The check
# is deliberately on the pair, not on the command: storing twelve different
# documents in one turn is ordinary work, whereas the same command with byte
# identical arguments twelve times in a minute is the flood signature and never
# something a user asked for twice.
#
# This lives in its own collection rather than in watcher_log. The log is a
# user-facing record that the Logs tab can clear, and dedupe state kept there
# would mean clearing the log silently re-armed every command in it — pressing
# "Clear" would cause the very flood the user pressed it to get rid of.
# watcher_recent is internal and nothing in the UI empties it.
REPEAT_COOLDOWN_S: float = 300.0    # 5 minutes
_RECENT_COLL = "watcher_recent"

# Identifies this process in watcher_processed.claimed_by, so it is obvious from
# the data which instance executed a given marker when several are pointed at
# the same database.
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

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
    # Results may be produced by a different instance than the one this client
    # is connected to, so delivery rides on a change stream rather than the
    # local bus alone. Started on first subscriber: an instance nobody is
    # browsing does not need the cursor.
    _ensure_broadcaster()
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
# Result broadcaster — cross-instance SSE delivery
# ---------------------------------------------------------------------------
# The push bus above is per-process. When more than one Falcon instance points
# at the same database (a local dev server plus a deployed one, or a single app
# scaled past one instance) the instance that executes a marker is usually not
# the one holding the browser's SSE connection, so an in-process push reaches
# nobody and the result only surfaces on the next history refetch.
#
# A change stream on the messages collection fixes that: every instance watches
# for injected results and fans them out to whichever subscribers it happens to
# hold. Delivery no longer depends on which instance did the work.
#
# Change streams need a replica set. Atlas always is one; a standalone mongod
# used for local dev is not, so we fall back to the direct in-process push there
# (correct for a single instance, which is the only thing a standalone supports
# anyway).

_broadcaster: Optional["ResultBroadcaster"] = None
_broadcaster_lock = threading.Lock()


def _change_streams_available() -> bool:
    """True if the deployment can serve a change stream (i.e. is a replica set)."""
    try:
        from falcon.db import supports_transactions
        return supports_transactions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("watcher: change-stream probe failed, assuming none: %s", exc)
        return False


class ResultBroadcaster(threading.Thread):
    """Tails the messages collection and fans injected results out to SSE queues."""

    # Only watcher-injected inserts matter; ordinary chat messages are delivered
    # by the request that created them.
    _PIPELINE = [{"$match": {"operationType": "insert", "fullDocument._watcher": True}}]

    def __init__(self) -> None:
        super().__init__(name="falcon-watcher-broadcaster", daemon=True)
        self._stop_event = threading.Event()
        self._stream = None

    def stop(self) -> None:
        self._stop_event.set()
        # Close the cursor so the blocking iteration returns promptly.
        stream = self._stream
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 — best effort
                pass

    def run(self) -> None:
        logger.info("watcher: result broadcaster started")
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                with get_db()["messages"].watch(self._PIPELINE) as stream:
                    self._stream = stream
                    backoff = 1.0  # connected — reset the retry delay
                    for change in stream:
                        if self._stop_event.is_set():
                            break
                        self._fan_out(change.get("fullDocument") or {})
            except Exception as exc:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                logger.error(
                    "watcher: broadcaster stream error (retrying in %.0fs): %s", backoff, exc,
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._stream = None
        logger.info("watcher: result broadcaster stopped")

    def _fan_out(self, doc: dict) -> None:
        identity_id = doc.get("identity_id")
        if not identity_id:
            return
        # A result condensed to nothing is tool plumbing the reader is not shown.
        # It is in the database for the model; there is no bubble to deliver.
        if not (doc.get("content") or "").strip():
            return
        _push_to_subscribers(identity_id, {
            "type": "watcher_result",
            "timestamp": doc.get("timestamp", ""),
            "content": doc.get("content", ""),
            "parent_ts": doc.get("_watcher_parent_ts", ""),
        })


def _ensure_broadcaster() -> None:
    """Start the broadcaster once per process. No-op without change streams."""
    global _broadcaster
    with _broadcaster_lock:
        if _broadcaster is not None and _broadcaster.is_alive():
            return
        if not _change_streams_available():
            logger.info(
                "watcher: no change-stream support, falling back to in-process result push",
            )
            return
        _broadcaster = ResultBroadcaster()
        _broadcaster.start()


def stop_result_broadcaster() -> None:
    """Stop the broadcaster. Called on FastAPI shutdown."""
    global _broadcaster
    with _broadcaster_lock:
        b, _broadcaster = _broadcaster, None
    if b:
        b.stop()


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


def format_result(result_text: str, command: str = "") -> str:
    """Wrap a tool result in the standard [AGENT RESULT] block.

    The command is named on its own line inside the block. It has to be: the
    model's own command block is stripped from the history it gets back (see
    ``falcon.identity._apply_agent_redaction``), so without this a turn that ran
    several tools would hand back several results with no way to tell which
    answered what. Inside the delimiters and therefore model-facing only — the
    reader never sees this block at all.
    """
    head = f"{_RESULT_OPEN}\n(from: {command.strip()})" if command.strip() else _RESULT_OPEN
    return f"{head}\n{result_text.strip()}\n{_RESULT_CLOSE}"


# Command blocks used to be rewritten as `(ran: <command>)` on the way into the
# history, by a `defuse_markers` function that lived here. It existed because the
# transcript handed back each turn was full of the model's own previous blocks —
# worked examples in the exact syntax the persona asks for, which it copied,
# which is what filled conversations with agent output.
#
# Nothing defuses them now because nothing has to: a command block never enters
# the stored message in the first place. `falcon.agent_redact` strips it as the
# reply streams, so `content` is written already clean and there is no second
# copy to leak. The command each result came from is named inside the result
# block by `format_result`, which is the one thing `(ran: ...)` was carrying.


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def _claim(msg_id: ObjectId, identity_id: str) -> bool:
    """Atomically claim a message for execution. True if this process won it.

    ``watcher_processed`` has a unique index on ``msg_id``, so a plain insert is
    the claim: exactly one caller can succeed and everyone else gets a duplicate
    key error. This has to be atomic rather than the old check-then-insert,
    because more than one Falcon instance can be pointed at the same database
    (a local dev server and a deployed one, say) and both poll the same
    ``messages`` collection. Under check-then-act both would execute the same
    marker — sending the tweet twice, spawning the agent twice.
    """
    try:
        get_db()["watcher_processed"].insert_one({
            "msg_id": msg_id,
            "identity_id": identity_id,
            "processed_at": _utc_now_iso(),
            "claimed_by": _INSTANCE_ID,
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception as exc:  # noqa: BLE001
        # Never execute a command we could not record — a failed claim on a
        # transient DB error is safer than a double send.
        logger.error("watcher: claim failed for msg %s: %s", msg_id, exc)
        return False


def _is_processed(msg_id: ObjectId) -> bool:
    db = get_db()
    return db["watcher_processed"].find_one({"msg_id": msg_id}) is not None


def _repeat_key(identity_id: str, command: str, payload: str) -> str:
    """Stable id for one (identity, command, payload). Hashed so an arbitrarily
    long payload still yields a bounded, indexable key."""
    h = hashlib.sha256(
        b"\x00".join(
            (identity_id.encode(), command.encode(), payload.encode())
        )
    ).hexdigest()
    return h


def _recently_run(identity_id: str, command: str, payload: str) -> Optional[str]:
    """When this exact command+payload last ran for this identity, if recent.

    Returns the timestamp of the previous run when it falls inside
    REPEAT_COOLDOWN_S, else None. `ran_at` is a fixed-width UTC ISO string, so a
    lexical comparison is a chronological one.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=REPEAT_COOLDOWN_S)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        prev = get_db()[_RECENT_COLL].find_one(
            {"_id": _repeat_key(identity_id, command, payload)}
        )
    except Exception as exc:  # noqa: BLE001
        # A failed lookup must not block real work — fall through and execute.
        logger.warning("watcher: repeat check failed for %r: %s", command, exc)
        return None
    if not prev:
        return None
    ran_at = prev.get("ran_at", "")
    return ran_at if ran_at >= cutoff else None


def _mark_run(identity_id: str, command: str, payload: str) -> None:
    """Record that this command+payload just ran, for the repeat check above."""
    try:
        get_db()[_RECENT_COLL].replace_one(
            {"_id": _repeat_key(identity_id, command, payload)},
            {
                "identity_id": identity_id,
                "command": command,
                "payload": payload[:500],
                "ran_at": _utc_now_iso(),
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("watcher: could not record run of %r: %s", command, exc)


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

def _inject_result(
    identity_id: str,
    result_text: str,
    trigger_msg_id: Optional[ObjectId] = None,
    trigger_ts: str = "",
    command: str = "",
    notice: Optional[str] = None,
) -> None:
    """Insert the watcher result as a distinct assistant message, then push to SSE subscribers.

    The result is written twice, because the model and the reader need different
    things from it. ``raw_content`` is the full ``[AGENT RESULT]`` block — the
    tables, ids, latencies and probe output the model has to reason over, and
    what history hands back next turn. ``content`` is what a person sees, which
    ``falcon.agent_redact.condense_result`` cuts down to one proof line, one
    failure sentence, or nothing at all. Only ``content`` leaves the server on
    any reader-facing path, so the dump has no way to reach the chat.

    A result condensed to nothing is still stored and still delivered to the
    model; it simply never surfaces. Nothing is pushed to SSE subscribers in
    that case — there is no message for them to draw.

    ``notice`` overrides the condensation with a caller-supplied single line, for
    a producer that knows its own one-line summary (research delivering a
    finished job). It is a substitute for the condensed line, never an escape
    from the filter: the raw body still goes only to ``raw_content``.

    ``_watcher_parent`` records which message carried the command. Insertion
    order alone cannot place the result correctly: a command can take minutes
    (research runs for several), and by the time it answers the user has often
    sent more messages, so appending puts the result far below the exchange it
    belongs to. Recording the parent lets the history be ordered so a result sits
    directly beneath its own command, wherever it finished.
    """
    from falcon.agent_redact import condense_result

    db = get_db()
    ts = _utc_now_iso()
    raw = format_result(result_text, command)
    visible = notice if notice is not None else condense_result(command, result_text)

    doc = {
        "identity_id": identity_id,
        "timestamp": ts,
        "role": "assistant",
        "content": visible,
        "raw_content": raw,
        "_watcher": True,
        # Which tool produced this, recorded as a field rather than left to be
        # parsed back out of the block. The history read needs it to decide what
        # the model is given: a read_document result goes to the reader and is
        # withheld from the payload, and that decision cannot be made from the
        # body alone. Server-side only — stripped before any client sees the row.
        "_watcher_command": (command or "").strip(),
    }
    if trigger_msg_id is not None:
        doc["_watcher_parent"] = trigger_msg_id
    # Carried on the document so the SSE fan-out can name the parent without a
    # second read — the broadcaster sees only the inserted document.
    if trigger_ts:
        doc["_watcher_parent_ts"] = trigger_ts
    db["messages"].insert_one(doc)

    if not visible:
        return   # nothing for a reader to see — no push, no empty bubble

    # With a broadcaster running, the insert above is itself the delivery signal
    # and every instance fans it out to its own subscribers — pushing here too
    # would deliver the result twice to clients attached to this instance.
    # Without change-stream support the local push is the only delivery path.
    if _broadcaster is not None and _broadcaster.is_alive():
        return

    _push_to_subscribers(identity_id, {
        "type": "watcher_result",
        "timestamp": ts,
        "content": visible,
        "parent_ts": trigger_ts,
    })


# ---------------------------------------------------------------------------
# Core scan + execute loop
# ---------------------------------------------------------------------------

def _process_message(identity_id: str, msg_doc: dict) -> None:
    """Scan one assistant message, execute any markers, inject results."""
    from falcon.watcher_tools import dispatch, set_current_identity

    # Markers are parsed out of the unredacted reply. ``content`` has had every
    # command block removed before storage so no reader-facing path can show
    # one, which means it is also the one field guaranteed to contain no marker
    # — reading it here would leave the watcher with nothing to run.
    content = msg_doc.get("raw_content") or msg_doc.get("content", "") or ""
    msg_id = msg_doc["_id"]

    # Guard: only assistant messages, not watcher result messages themselves
    if msg_doc.get("role") != "assistant":
        return
    if msg_doc.get("_watcher"):
        return  # skip our own injected results

    markers = parse_markers(content)
    if not markers:
        return

    # Claim BEFORE execution: this both prevents a crash mid-run from causing a
    # retry and guarantees only one instance runs the markers in this message.
    if not _claim(msg_id, identity_id):
        logger.info(
            "watcher: msg %s already claimed by another instance, skipping", msg_id,
        )
        return

    logger.info(
        "watcher: found %d marker(s) in msg %s for identity=%r",
        len(markers), msg_id, identity_id,
    )

    # Tools that own per-user state (research jobs) need to know whose
    # conversation they are serving; without this they would scope to nobody and
    # every identity would see every other identity's work. The message id goes
    # with it so post_tweet can refuse a confirmation issued in the same turn
    # that staged the tweet — the human has to have had a turn in between.
    set_current_identity(identity_id, str(msg_id))

    for m in markers:
        command = m["command"]
        payload = m["payload"]

        # Same command, same arguments, already run moments ago: the model has
        # repeated itself (it can see its own past markers in the history it is
        # given). Running it again would only add another copy of an answer the
        # conversation already contains, so skip it — and inject nothing, since
        # a result is exactly what we are trying not to pile up.
        prev_at = _recently_run(identity_id, command, payload)
        if prev_at is not None:
            logger.info(
                "watcher: skipping repeat of %r for identity=%r — identical run at %s",
                command, identity_id, prev_at,
            )
            continue

        t0 = time.monotonic()
        result = dispatch(command, payload)
        latency_ms = round((time.monotonic() - t0) * 1000)
        error = result.startswith("[ERROR]") or result.startswith("[NOT CONFIGURED]")

        # Only a success suppresses a later retry — a failed command should stay
        # runnable, or a transient outage would lock the user out of it.
        if not error:
            _mark_run(identity_id, command, payload)

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
        _inject_result(
            identity_id, result,
            trigger_msg_id=msg_id,
            trigger_ts=msg_doc.get("timestamp", ""),
            command=command,
        )


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
                # The claim inside _process_message already blocks a retry; this
                # covers a failure raised before it got that far.
                _claim(msg["_id"], self.identity_id)


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


# ---------------------------------------------------------------------------
# Watcher Persona — dynamic, shared across all watcher-enabled identities
# ---------------------------------------------------------------------------

def get_watcher_persona() -> str:
    """The watcher persona the model sees, assembled fresh on every call.

    The authored halves come from MongoDB and the AVAILABLE COMMANDS block is
    rebuilt from the live tool registry, so a tool spawned or deleted by any
    process — or by this one a second ago — is reflected immediately and cannot
    go stale. Returns empty string on failure rather than raising, since a
    missing persona degrades the turn but must not break it.
    """
    try:
        import falcon.watcher_persona as Persona

        return Persona.assemble()
    except Exception as exc:
        logger.error("watcher: get_watcher_persona failed: %s", exc)
        return ""


def refresh_watcher_persona(new_tool_name: str = "", new_tool_context: str = "") -> None:
    """Kept for call sites that fire after the tool registry changes.

    Nothing needs rebuilding any more: the command list is derived at read time,
    so a newly spawned or deleted tool is already reflected the next time the
    persona is assembled. This exists so spawn_agent, agent deletion and startup
    keep working unchanged, and to log the transition.

    The arguments are ignored — the new tool is already in the registry and in
    the generated-tool store by the time this is called.
    """
    try:
        import falcon.watcher_tools as WatcherTools

        tools = len(WatcherTools.list_tools())
        logger.info(
            "watcher: persona now advertises %d tool(s)%s",
            tools,
            f" after registering {new_tool_name!r}" if new_tool_name else "",
        )
    except Exception as exc:
        logger.error("watcher: refresh_watcher_persona failed: %s", exc)

