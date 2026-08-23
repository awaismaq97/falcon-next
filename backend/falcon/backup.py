"""
backup.py — periodic in-cluster snapshot of the Falcon database.

Copies every collection from the live database into a sibling database on the
same Atlas cluster (``falcon`` → ``falcon_backup``), on a schedule, replacing
the previous snapshot each time. One connection string, no external tooling,
nothing to install.

What this protects against
    Application-level data loss: a bad delete, a buggy migration, a generated
    watcher tool dropping a collection. That is the failure mode this codebase
    is actually exposed to, since generated tools execute with full builtins
    and can reach Mongo directly.

What it does NOT protect against
    Loss of the cluster or the Atlas account — the copy lives on the same
    cluster, so anything that takes out the cluster takes out both. It is also
    a single generation: a corruption that goes unnoticed for a full interval
    gets copied over the good snapshot. Pair it with Atlas Cloud Backup (paid
    tiers) or an off-cluster dump if that matters.

Schedule
    A daemon thread checks every 15 minutes whether a run is due. "Due" is
    computed from the last *successful* run recorded in Mongo, not from process
    start, so container restarts and redeploys do not reset the clock and do not
    cause a re-run.

Configuration (all optional)
    BACKUP_ENABLED         "false" to disable entirely.        Default: enabled
    BACKUP_DB              Target database name.  Default: "<source>_backup"
    BACKUP_INTERVAL_DAYS   Days between runs.                  Default: 7

Manual run, useful for verifying the setup without waiting a week::

    python -m falcon.backup          # run now if due
    python -m falcon.backup --force  # run now regardless
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from falcon.db import get_client, get_db

logger = logging.getLogger("falcon.backup")

# Bookkeeping lives in the *source* database: the target is wiped and rebuilt
# on every run, so it cannot hold the record of when that last happened.
_STATE_COLL = "backup_state"
_LOG_COLL = "backup_runs"
_SINGLETON_ID = "singleton"

# Collections are staged under this prefix and renamed into place, so a run
# that dies halfway leaves the previous snapshot of the remaining collections
# intact rather than a half-populated one.
_STAGING_PREFIX = "__staging__"

_CHECK_INTERVAL_SECONDS = 15 * 60
_STARTUP_GRACE_SECONDS = 120
# A crashed process leaves its claim behind. After this long, assume the holder
# is gone rather than blocking backups forever.
_STALE_CLAIM = timedelta(hours=2)
# Kept modest on purpose: the production instance is 1 vCPU / 1 GB, and
# audit_log holds full inference records. Peak memory during a copy is roughly
# batch size × document size, held twice while BSON is encoded, so a large
# batch of large documents is the one way this could pressure the container.
_BATCH = 500
_KEEP_RUN_RECORDS = 20

_thread: threading.Thread | None = None
_stop = threading.Event()
_thread_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Configuration ──────────────────────────────────────────────────────────

def enabled() -> bool:
    raw = os.environ.get("BACKUP_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def interval_days() -> int:
    raw = os.environ.get("BACKUP_INTERVAL_DAYS", "").strip()
    if not raw:
        return 7
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("BACKUP_INTERVAL_DAYS=%r is not an integer, using 7", raw)
        return 7


def backup_db_name() -> str:
    """Target database name — configured, or the source name plus '_backup'."""
    configured = os.environ.get("BACKUP_DB", "").strip()
    return configured or f"{get_db().name}_backup"


# ── Scheduling state ───────────────────────────────────────────────────────

def last_success() -> datetime | None:
    doc = get_db()[_STATE_COLL].find_one({"_id": _SINGLETON_ID})
    if not doc:
        return None
    ts = doc.get("last_success_at")
    if not isinstance(ts, datetime):
        return None
    # PyMongo returns naive UTC datetimes unless the client is tz-aware.
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def is_due() -> bool:
    last = last_success()
    if last is None:
        return True
    return _now() - last >= timedelta(days=interval_days())


def status() -> dict[str, Any]:
    """Current backup configuration and history — for logs and diagnostics."""
    last = last_success()
    recent = list(
        get_db()[_LOG_COLL].find({}, {"_id": 0}).sort("started_at", -1).limit(5)
    )
    return {
        "enabled": enabled(),
        "source_db": get_db().name,
        "backup_db": backup_db_name(),
        "interval_days": interval_days(),
        "last_success_at": last.isoformat() if last else None,
        "due": is_due(),
        "recent_runs": recent,
    }


def _claim() -> bool:
    """Take the run lock. False if another process holds a fresh claim.

    Guards the case of two instances pointed at one database — the same hazard
    the watcher has. Not a hard lock: a stale claim is stolen after
    ``_STALE_CLAIM`` so a crashed process cannot wedge backups permanently.
    """
    coll = get_db()[_STATE_COLL]
    cutoff = _now() - _STALE_CLAIM
    try:
        result = coll.update_one(
            {
                "_id": _SINGLETON_ID,
                "$or": [
                    {"running": {"$ne": True}},
                    {"claimed_at": {"$lt": cutoff}},
                ],
            },
            {"$set": {"running": True, "claimed_at": _now(), "claimed_by": os.getpid()}},
            upsert=True,
        )
    except DuplicateKeyError:
        # The singleton exists but the filter excluded it, so upsert tried to
        # insert a second one. That is exactly the "someone else holds a fresh
        # claim" case.
        return False
    return bool(result.matched_count or result.upserted_id is not None)


def _release(success: bool) -> None:
    update: dict[str, Any] = {"running": False, "released_at": _now()}
    if success:
        update["last_success_at"] = _now()
    get_db()[_STATE_COLL].update_one({"_id": _SINGLETON_ID}, {"$set": update}, upsert=True)


# ── Copying ────────────────────────────────────────────────────────────────

def _copy_collection(src: Database, dst: Database, name: str) -> int:
    """Copy one collection into ``dst``, replacing any previous copy.

    Writes to a staging collection and renames it over the target, so the old
    copy survives right up to the moment a complete new one exists.
    """
    source = src[name]
    expected = source.count_documents({})

    staging_name = f"{_STAGING_PREFIX}{name}"
    staging = dst[staging_name]
    staging.drop()

    copied = 0
    batch: list[dict] = []
    for doc in source.find({}).batch_size(_BATCH):
        batch.append(doc)
        if len(batch) >= _BATCH:
            staging.insert_many(batch, ordered=False)
            copied += len(batch)
            batch = []
    if batch:
        staging.insert_many(batch, ordered=False)
        copied += len(batch)

    # The source is live, so counts drifting by a few documents mid-copy is
    # normal and not a failure. Copying nothing out of a populated collection
    # is not — that is the shape of a broken run, and it must not be allowed to
    # replace a good snapshot.
    if expected > 0 and copied == 0:
        staging.drop()
        raise RuntimeError(f"{name}: source has {expected} documents but none were copied")
    if copied != expected:
        logger.info("backup: %s copied %d of %d (collection is live)", name, copied, expected)

    if copied == 0:
        # Nothing was written, so no staging collection exists to rename — and
        # renaming a missing namespace would fail into the fallback path below
        # and log a misleading warning. Just clear the previous copy.
        dst[name].drop()
        return 0

    try:
        staging.rename(name, dropTarget=True)
    except OperationFailure as exc:
        # Shared Atlas tiers can withhold renameCollection. Fall back to a
        # drop-and-refill of the target, which is not atomic but still ends in
        # a complete copy.
        logger.warning("backup: rename unavailable (%s), falling back to drop+refill for %s", exc, name)
        target = dst[name]
        target.drop()
        moved = 0
        batch = []
        for doc in staging.find({}).batch_size(_BATCH):
            batch.append(doc)
            if len(batch) >= _BATCH:
                target.insert_many(batch, ordered=False)
                moved += len(batch)
                batch = []
        if batch:
            target.insert_many(batch, ordered=False)
            moved += len(batch)
        staging.drop()
        copied = moved

    return copied


def run_backup(force: bool = False) -> dict[str, Any]:
    """Replace the backup database with a fresh copy of the live one.

    Indexes are deliberately not copied. They carry no data, and db.py rebuilds
    every one of them at startup — including the unique index on
    watcher_processed.msg_id — as soon as a restored database is served.
    """
    client = get_client()
    src = get_db()
    src_name = src.name
    dst_name = backup_db_name()

    if dst_name == src_name:
        raise ValueError(f"BACKUP_DB is {dst_name!r}, the same as the live database — refusing to run")
    if src_name.endswith("_backup") and not force:
        raise ValueError(f"live database is {src_name!r}, which looks like a backup — refusing to back up a backup")

    dst = client[dst_name]

    skip = {_STATE_COLL, _LOG_COLL}
    names = sorted(
        n for n in src.list_collection_names()
        if n not in skip and not n.startswith(_STAGING_PREFIX)
    )

    # Never let an empty or unreachable source wipe an existing snapshot.
    if not names and dst.list_collection_names():
        raise RuntimeError(f"source database {src_name!r} has no collections — refusing to overwrite the snapshot")

    if not _claim():
        raise RuntimeError("another process is already running a backup")

    started = _now()
    record: dict[str, Any] = {
        "started_at": started,
        "source_db": src_name,
        "backup_db": dst_name,
        "pid": os.getpid(),
    }
    counts: dict[str, int] = {}

    try:
        for name in names:
            counts[name] = _copy_collection(src, dst, name)

        # Mirror deletions: a collection dropped from the source should not
        # linger in the snapshot, or a restore would resurrect it. Bookkeeping
        # collections are exempt — they are skipped on the way in, and a restore
        # (which runs this in reverse) must not wipe the schedule state.
        for stale in dst.list_collection_names():
            if stale not in counts and stale not in skip:
                dst[stale].drop()
                logger.info("backup: dropped %s from snapshot (gone from source)", stale)

        record.update(
            status="ok",
            finished_at=_now(),
            duration_seconds=round((_now() - started).total_seconds(), 1),
            collections=len(counts),
            documents=sum(counts.values()),
            per_collection=counts,
        )
        logger.info(
            "backup: %s → %s, %d collections, %d documents in %.1fs",
            src_name, dst_name, len(counts), sum(counts.values()), record["duration_seconds"],
        )
        _release(success=True)
        return record

    except Exception as exc:
        record.update(
            status="error",
            finished_at=_now(),
            error=str(exc),
            collections_completed=len(counts),
        )
        logger.error("backup: failed after %d collections: %s", len(counts), exc)
        # Deliberately not marking a success timestamp, so the next check retries.
        _release(success=False)
        raise
    finally:
        try:
            log = get_db()[_LOG_COLL]
            log.insert_one(dict(record))
            old = list(log.find({}, {"_id": 1}).sort("started_at", -1).skip(_KEEP_RUN_RECORDS))
            if old:
                log.delete_many({"_id": {"$in": [d["_id"] for d in old]}})
        except Exception as log_exc:  # noqa: BLE001 — logging must never mask the run result
            logger.warning("backup: could not record run: %s", log_exc)


# ── Scheduler ──────────────────────────────────────────────────────────────

def _loop() -> None:
    # Let startup settle before touching the database — a full copy competing
    # with index creation and watcher bootstrap slows the first requests.
    if _stop.wait(_STARTUP_GRACE_SECONDS):
        return
    while not _stop.is_set():
        try:
            if enabled() and is_due():
                run_backup()
        except Exception as exc:  # noqa: BLE001 — the scheduler must survive a failed run
            logger.error("backup: scheduled run failed, will retry: %s", exc)
        if _stop.wait(_CHECK_INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    """Start the background backup thread. Idempotent."""
    global _thread
    if not enabled():
        logger.info("backup: disabled via BACKUP_ENABLED")
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="falcon-backup", daemon=True)
        _thread.start()
    last = last_success()
    logger.info(
        "backup: scheduler started — %s → %s every %d day(s), last success %s",
        get_db().name, backup_db_name(), interval_days(),
        last.isoformat() if last else "never",
    )


def stop_scheduler() -> None:
    """Signal the background thread to exit. Called on shutdown."""
    global _thread
    _stop.set()
    with _thread_lock:
        t = _thread
        _thread = None
    if t is not None and t.is_alive():
        t.join(timeout=5)


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    force = "--force" in sys.argv

    if "--status" in sys.argv:
        print(json.dumps(status(), indent=2, default=str))
    elif force or is_due():
        result = run_backup(force=force)
        print(json.dumps(result, indent=2, default=str))
    else:
        last = last_success()
        print(f"Not due — last successful backup {last.isoformat() if last else 'never'}. Use --force to run anyway.")
