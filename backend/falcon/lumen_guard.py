"""
lumen_guard.py — Lumen Guard: a health monitor for the running system.

It answers one question, continuously: is each part of Falcon working and
intact? Database, auth, watcher, tools, persona, storage, the weekly backup, and
the background workers. Each check runs a real operation rather than reading a
flag — a database that answers a ping but cannot complete a write is not
working, a watcher that is enabled but has no thread is not running, and a
backup whose snapshot database is empty has not been taken however successful
its last run claims to be.

Every check returns one of three states:

    ok    — verified working just now
    warn  — working, but something is off and someone should look
    down  — not working

The overall state is the worst of them. A background thread re-runs everything
every 60 seconds, logs at ERROR when anything is down, and stores the latest
result so the admin panel and any other worker can read it.

Admin only. The routes live in app/routers/lumen.py behind require_admin, and
this is deliberately not a watcher tool — the model cannot call it, and the
checks name identities and account state that a portal user should not see.

Configuration (all optional)
    LUMEN_ENABLED            "false" to keep the monitor from starting.
    LUMEN_INTERVAL_SECONDS   Seconds between runs. Default 60, floor 15.
    LUMEN_STARTUP_GRACE      Seconds to wait before the first run. Default 20.

Manual use::

    python -m falcon.lumen_guard          # run the checks, print them
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from falcon.db import get_db

logger = logging.getLogger("falcon.lumen")

# The one document Lumen writes: the most recent result, so the panel and other
# workers can see it without each of them re-running every check.
STATE_COLL = "lumen_state"
_SINGLETON_ID = "singleton"

OK, WARN, DOWN = "ok", "warn", "down"
_ORDER = {OK: 0, WARN: 1, DOWN: 2}

# Windows and thresholds for the checks that look at recent activity.
FAILED_LOGIN_WINDOW_MINUTES = 15
FAILED_LOGIN_WARN = 5
TOOL_ERROR_WINDOW_MINUTES = 30
TOOL_ERROR_MIN_SAMPLE = 5
TOOL_ERROR_WARN_RATIO = 0.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_ago(minutes: int) -> str:
    """An ISO-Z cutoff. watcher_log and admin_audit_log store timestamps as
    ISO-Z text, which compares lexically — so a string cutoff is a correct range
    query against them."""
    return (_now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _result(name: str, state: str, message: str, facts: dict[str, Any] | None = None) -> dict:
    return {"name": name, "state": state, "message": message, "facts": facts or {}}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

_CHECKS: list[tuple[str, Callable[[], dict]]] = []


def check(name: str) -> Callable:
    def _dec(fn: Callable[[], dict]):
        _CHECKS.append((name, fn))
        return fn
    return _dec


@check("database")
def _check_database() -> dict:
    """Ping, then actually write, read back and delete.

    A ping only proves a socket. The failure worth catching is storage that
    accepts a connection but cannot complete a write, which looks completely
    healthy from the outside and silently loses everything.
    """
    started = time.monotonic()
    db = get_db()
    db.client.admin.command("ping")
    ping_ms = round((time.monotonic() - started) * 1000, 1)

    coll = db[STATE_COLL]
    token = f"probe-{_now().timestamp()}"
    started = time.monotonic()
    coll.replace_one({"_id": "healthcheck"}, {"_id": "healthcheck", "token": token}, upsert=True)
    back = coll.find_one({"_id": "healthcheck"})
    write_ms = round((time.monotonic() - started) * 1000, 1)

    if not back or back.get("token") != token:
        return _result("database", DOWN,
                       "The database accepted a write but did not read it back. Data is not "
                       "being stored.",
                       {"database": db.name, "ping_ms": ping_ms})

    return _result("database", OK,
                   f"Connected to {db.name}. Write and read-back verified.",
                   {"database": db.name, "ping_ms": ping_ms, "write_ms": write_ms})


@check("auth")
def _check_auth() -> dict:
    """Verify the machinery that logs people in, without touching any secret value."""
    from falcon import admin_auth as A
    import falcon.admin_users as AU

    facts: dict[str, Any] = {}

    key = A.SECRET_KEY or ""
    facts["secret_key_length"] = len(key)          # length only, never the value
    if not key:
        return _result("auth", DOWN,
                       "SECRET_KEY is not set. Nobody can log in and no session can be "
                       "verified.", facts)
    if len(key) < 32:
        return _result("auth", WARN,
                       f"SECRET_KEY is only {len(key)} characters. It should be at least 32.",
                       facts)

    # Round-trip the two primitives every login depends on.
    try:
        if A.decrypt_username(A.encrypt_username("healthcheck")) != "healthcheck":
            raise ValueError("username did not survive the round trip")
        facts["username_encryption"] = "verified"
    except Exception as exc:  # noqa: BLE001
        return _result("auth", DOWN,
                       f"Username encryption is broken: {type(exc).__name__}: {exc}. Logins "
                       "cannot resolve an account.", facts)

    try:
        token = A.create_access_token("healthcheck", "healthcheck", "admin", "default")
        claims = A.decode_access_token(token)
        if claims.get("username") != "healthcheck":
            raise ValueError("token did not survive the round trip")
        facts["session_tokens"] = "verified"
    except Exception as exc:  # noqa: BLE001
        return _result("auth", DOWN,
                       f"Session tokens are broken: {type(exc).__name__}: {exc}. Logging in "
                       "would not produce a usable session.", facts)

    admins = AU.list_admins()
    facts["admin_accounts"] = len(admins)
    if not admins:
        return _result("auth", DOWN,
                       "No admin account exists. Nobody can reach the admin panel.", facts)

    failed = get_db()["admin_audit_log"].count_documents({
        "action": "login_failed",
        "timestamp": {"$gte": _iso_ago(FAILED_LOGIN_WINDOW_MINUTES)},
    })
    facts["failed_logins_recent"] = failed
    facts["window_minutes"] = FAILED_LOGIN_WINDOW_MINUTES
    if failed >= FAILED_LOGIN_WARN:
        return _result("auth", WARN,
                       f"Login works, but {failed} logins have failed in the last "
                       f"{FAILED_LOGIN_WINDOW_MINUTES} minutes.", facts)

    return _result("auth", OK,
                   f"Encryption, session tokens and {len(admins)} admin account"
                   f"{'s' if len(admins) != 1 else ''} all verified.", facts)


@check("watcher")
def _check_watcher() -> dict:
    """Are the watcher threads that should be running actually running?"""
    import falcon.watcher as W

    enabled = sorted(W.get_enabled_identities())
    prefix = "falcon-watcher-"
    running = sorted(
        t.name[len(prefix):]
        for t in threading.enumerate()
        if t.name.startswith(prefix) and t.name != "falcon-watcher-broadcaster"
    )
    broadcaster = any(t.name == "falcon-watcher-broadcaster" for t in threading.enumerate())

    facts = {
        "enabled": enabled,
        "running": running,
        "broadcaster": broadcaster,
        "pid": os.getpid(),
    }

    if not enabled:
        return _result("watcher", OK, "No identity has the watcher enabled.", facts)

    missing = [i for i in enabled if i not in running]
    extra = [i for i in running if i not in enabled]

    if extra:
        return _result("watcher", WARN,
                       f"A watcher is running for {', '.join(extra)}, which is not enabled. "
                       "It is still reading and acting on that conversation.", facts)
    if missing and not running:
        return _result("watcher", DOWN,
                       f"{len(missing)} identity/identities have the watcher enabled but no "
                       "watcher thread is running here. Commands are not being executed.",
                       facts)
    if missing:
        return _result("watcher", WARN,
                       f"Watchers are running, but not for {', '.join(missing)}.", facts)

    return _result("watcher", OK,
                   f"{len(running)} watcher{'s' if len(running) != 1 else ''} running "
                   f"({', '.join(running)}).", facts)


@check("tools")
def _check_tools() -> dict:
    """Dispatch a real command end to end, and report the recent failure rate."""
    import falcon.watcher_tools as WT

    facts: dict[str, Any] = {}
    try:
        tools = WT.list_tools()
        facts["count"] = len(tools)
        facts["tools"] = tools
    except Exception as exc:  # noqa: BLE001
        return _result("tools", DOWN,
                       f"The tool registry could not be read: {type(exc).__name__}: {exc}",
                       facts)

    # ping is the cheapest tool that proves the dispatch path works: no network,
    # no database, no side effect.
    out = WT.dispatch("ping", "")
    if out.startswith("[ERROR]"):
        return _result("tools", DOWN,
                       f"Dispatching a command failed: {out[:120]}", facts)
    facts["dispatch"] = "verified"

    cutoff = _iso_ago(TOOL_ERROR_WINDOW_MINUTES)
    log = get_db()["watcher_log"]
    total = log.count_documents({"recorded_at": {"$gte": cutoff}})
    errors = log.count_documents({"recorded_at": {"$gte": cutoff}, "error": True})
    facts["recent_calls"] = total
    facts["recent_errors"] = errors
    facts["window_minutes"] = TOOL_ERROR_WINDOW_MINUTES

    if total >= TOOL_ERROR_MIN_SAMPLE and errors / total >= TOOL_ERROR_WARN_RATIO:
        failing = [c for c in log.distinct("command", {
            "recorded_at": {"$gte": cutoff}, "error": True}) if c]
        facts["failing_commands"] = sorted(failing)[:10]
        return _result("tools", WARN,
                       f"{len(tools)} tools available, but {errors} of the last {total} calls "
                       f"failed ({', '.join(sorted(failing)[:4])}).", facts)

    return _result("tools", OK, f"{len(tools)} tools available and dispatching.", facts)


@check("persona")
def _check_persona() -> dict:
    """The watcher persona is what tells the model the tools exist at all."""
    import falcon.watcher_persona as P

    text = P.assemble()
    facts = {"chars": len(text)}
    if not text.strip():
        return _result("persona", DOWN,
                       "The watcher persona is empty. The model has no instructions and will "
                       "not emit commands.", facts)
    if "AVAILABLE COMMANDS" not in text:
        return _result("persona", WARN,
                       "The persona is present but carries no command list.", facts)

    parts = P.get_parts()
    facts["updated_by"] = parts.get("updated_by", "")
    facts["rules_chars"] = len(parts.get("rules", "") or "")
    if not (parts.get("rules") or "").strip():
        return _result("persona", WARN,
                       "The persona has no rules section. The model is running without its "
                       "written constraints.", facts)

    return _result("persona", OK,
                   f"Assembled, {len(text):,} characters, command list present.", facts)


@check("storage")
def _check_storage() -> dict:
    """Stored documents, and whether any of them point at files that are gone."""
    coll = get_db()["stored_documents"]
    total = coll.count_documents({})
    with_file = coll.count_documents({"file_id": {"$exists": True, "$ne": ""}})
    facts: dict[str, Any] = {"documents": total, "with_file": with_file}

    dangling: list[str] = []
    try:
        from falcon import file_store

        for doc in coll.find({"file_id": {"$exists": True, "$ne": ""}},
                             {"_id": 0, "storage_id": 1, "file_id": 1}).limit(500):
            if not file_store.exists(doc.get("file_id", "")):
                dangling.append(doc.get("storage_id", "?"))
    except Exception as exc:  # noqa: BLE001
        return _result("storage", WARN,
                       f"{total} documents stored, but the file check could not run: "
                       f"{type(exc).__name__}: {exc}", facts)

    if dangling:
        facts["dangling"] = dangling[:20]
        return _result("storage", WARN,
                       f"{len(dangling)} document(s) point at files that are missing. Their "
                       "download links produce nothing.", facts)

    return _result("storage", OK,
                   f"{total} document{'s' if total != 1 else ''} stored, "
                   f"{with_file} with a downloadable file.", facts)


@check("backup")
def _check_backup() -> dict:
    """The weekly snapshot: is it scheduled, did it run, and is the copy really there?

    A backup is the one subsystem where "the scheduler is running" proves almost
    nothing. What matters is when a run last *succeeded* and whether the snapshot
    it claims to have written actually exists — a recorded success pointing at an
    empty database is worse than no backup at all, because it is believed.
    """
    from falcon import backup as B
    from falcon.db import get_client

    interval = B.interval_days()
    target = B.backup_db_name()
    scheduler_alive = any(t.name == "falcon-backup" for t in threading.enumerate())

    facts: dict[str, Any] = {
        "enabled": B.enabled(),
        "interval_days": interval,
        "backup_database": target,
        "scheduler_running": scheduler_alive,
    }

    if not B.enabled():
        return _result("backup", WARN,
                       "Backups are switched off (BACKUP_ENABLED). Nothing is being copied.",
                       facts)

    last = B.last_success()
    facts["last_success"] = last.strftime("%Y-%m-%d %H:%M UTC") if last else None

    # What is actually in the snapshot right now.
    snapshot_collections: int | None = None
    try:
        snapshot_collections = len(get_client()[target].list_collection_names())
        facts["snapshot_collections"] = snapshot_collections
    except Exception as exc:  # noqa: BLE001
        facts["snapshot_error"] = f"{type(exc).__name__}: {exc}"

    # The most recent run, successful or not — a failing run is invisible in the
    # success timestamp, which simply stops moving.
    try:
        recent = get_db()[B._LOG_COLL].find_one({}, {"_id": 0}, sort=[("started_at", -1)])
    except Exception:  # noqa: BLE001
        recent = None
    if recent:
        facts["last_run"] = recent.get("status", "?")
        if recent.get("status") == "ok":
            facts["last_run_documents"] = recent.get("documents")

    age_days = ((_now() - last).total_seconds() / 86400) if last else None
    if age_days is not None:
        facts["days_since"] = round(age_days, 1)

    # A recorded success whose snapshot is not there. Believed, and wrong.
    if last is not None and snapshot_collections == 0:
        return _result("backup", DOWN,
                       f"A backup is recorded as successful, but {target} is empty. There is "
                       "no restorable copy.", facts)

    if age_days is not None and age_days > interval * 2:
        return _result("backup", DOWN,
                       f"The last successful backup was {age_days:.0f} days ago, more than "
                       f"twice the {interval}-day schedule. Backups are not running.", facts)

    if recent and recent.get("status") == "error":
        err = str(recent.get("error", ""))[:160]
        return _result("backup", WARN,
                       f"The most recent backup run failed: {err}", facts)

    if age_days is not None and age_days > interval:
        return _result("backup", WARN,
                       f"The last successful backup was {age_days:.1f} days ago; the schedule "
                       f"is every {interval} days. It is overdue.", facts)

    if last is None:
        return _result("backup", WARN,
                       "No backup has completed yet. The first one runs shortly after "
                       "startup." if scheduler_alive else
                       "No backup has completed yet, and the scheduler is not running.", facts)

    if not scheduler_alive:
        return _result("backup", WARN,
                       f"Last backup {facts['last_success']}, but the scheduler is not running "
                       "in this process, so the next one will not fire from here.", facts)

    due_in = max(0.0, interval - age_days)
    return _result("backup", OK,
                   f"Last backup {facts['last_success']} ({age_days:.1f} days ago), "
                   f"{snapshot_collections} collections in {target}. Next due in "
                   f"{due_in:.1f} days.", facts)


@check("workers")
def _check_workers() -> dict:
    """The background threads that keep research and this monitor going.

    Backup has its own check — a thread being alive says nothing useful about
    whether data is actually being copied.
    """
    names = {t.name for t in threading.enumerate()}
    expected = {"research": "falcon-research", "monitor": "falcon-lumen"}
    facts: dict[str, Any] = {label: (thread in names) for label, thread in expected.items()}
    facts["thread_count"] = threading.active_count()

    stopped = [label for label in expected if not facts[label]]
    if stopped:
        return _result("workers", WARN, f"Not running: {', '.join(stopped)}.", facts)

    return _result("workers", OK,
                   f"All background workers running ({threading.active_count()} threads).",
                   facts)


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------

def run_checks() -> dict[str, Any]:
    """Run every check and return the report. Also stores it as the latest result."""
    started = time.monotonic()
    results: list[dict] = []

    for name, fn in _CHECKS:
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001
            # A check that raises is a failure of the thing it checks, as far as
            # anyone can tell from here. Reporting "unknown" would be worse.
            logger.error("lumen: check %r raised: %s", name, exc)
            results.append(_result(
                name, DOWN,
                f"The check could not complete: {type(exc).__name__}: {exc}",
            ))

    overall = OK
    for r in results:
        if _ORDER[r["state"]] > _ORDER[overall]:
            overall = r["state"]

    report = {
        "overall": overall,
        "checked_at": _now(),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "pid": os.getpid(),
        "checks": results,
        "counts": {
            OK: sum(1 for r in results if r["state"] == OK),
            WARN: sum(1 for r in results if r["state"] == WARN),
            DOWN: sum(1 for r in results if r["state"] == DOWN),
        },
    }

    try:
        get_db()[STATE_COLL].replace_one(
            {"_id": _SINGLETON_ID}, {"_id": _SINGLETON_ID, **report}, upsert=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("lumen: could not store the result: %s", exc)

    broken = [r["name"] for r in results if r["state"] == DOWN]
    if broken:
        logger.error("lumen: NOT WORKING — %s", ", ".join(broken))
    elif overall == WARN:
        logger.warning("lumen: %s", "; ".join(
            f"{r['name']}: {r['message']}" for r in results if r["state"] == WARN))

    return report


def last_result() -> dict[str, Any] | None:
    """The most recent stored report, or None if nothing has run yet."""
    try:
        doc = get_db()[STATE_COLL].find_one({"_id": _SINGLETON_ID}, {"_id": 0})
        return doc or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# The background monitor
# ---------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_thread_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("LUMEN_ENABLED", "true").strip().lower() not in ("false", "0", "no")


def interval_seconds() -> int:
    try:
        return max(15, int(os.environ.get("LUMEN_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


def _startup_grace() -> int:
    try:
        return max(0, int(os.environ.get("LUMEN_STARTUP_GRACE", "20")))
    except ValueError:
        return 20


def _loop() -> None:
    # Let boot settle first: watchers, index building and the research worker are
    # all still starting, and a check during that reports half of them as down
    # and then clears itself a minute later.
    if _stop.wait(_startup_grace()):
        return
    while not _stop.is_set():
        try:
            run_checks()
        except Exception as exc:  # noqa: BLE001 — the monitor must outlive a bad run
            logger.error("lumen: check run failed, will retry: %s", exc)
        if _stop.wait(interval_seconds()):
            return


def start_monitor() -> None:
    """Start the background monitor. Idempotent."""
    global _thread
    if not enabled():
        logger.info("lumen: disabled via LUMEN_ENABLED")
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="falcon-lumen", daemon=True)
        _thread.start()
    logger.info("lumen: monitor started — checking every %ds", interval_seconds())


def stop_monitor() -> None:
    """Signal the monitor to exit. Called on shutdown."""
    global _thread
    _stop.set()
    with _thread_lock:
        t = _thread
        _thread = None
    if t is not None and t.is_alive():
        t.join(timeout=5)


def is_running() -> bool:
    with _thread_lock:
        t = _thread
    return t is not None and t.is_alive()


def status() -> dict[str, Any]:
    """What the admin panel shows: the monitor's own state plus the last result."""
    return {
        "enabled": enabled(),
        "running": is_running(),
        "interval_seconds": interval_seconds(),
        "pid": os.getpid(),
        "checks": [name for name, _ in _CHECKS],
        "last": last_result(),
    }


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    report = run_checks()
    print(json.dumps(report, indent=2, default=str))
