"""
db.py — MongoDB connection singleton for Falcon (FastAPI backend).

Provides a single get_db() function that returns the Falcon database handle.
The MongoClient is created once per process and reused across all callers and
all request worker threads.

This is the FastAPI-native version: it has NO dependency on Streamlit. The
client is memoised in a module-level global guarded by a lock instead of
st.cache_resource. Indexes are still built once, in a background daemon thread,
so the first request is never blocked on ~15 sequential create_index calls.

Requires MONGODB_URI in the environment (loaded by config.py before this
module is imported).

Collections used:
  identities             — {identity_id, created_at}
  messages               — {identity_id, timestamp, role, content}
  memory                 — {identity_id, memory_type, content, tags, pinned, source, ...}
  traces                 — {identity_id, user_timestamp, send_timestamp, user, steps}
  tokens                 — {identity_id, prompt, completion, total}
  audit_log              — full inference audit records
  conversation_summaries — {identity_id, summary, turn_count, updated_at}
  dual_run_log           — side-by-side dual-run records
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

from pymongo import MongoClient
from pymongo.client_session import ClientSession
from pymongo.database import Database

logger = logging.getLogger(__name__)

# Process-wide singletons. The MongoClient is thread-safe and manages its own
# connection pool, so one instance is shared across every FastAPI worker thread.
_client: MongoClient | None = None
_db: Database | None = None
_client_lock = threading.Lock()

# Ensures the one-time index build runs at most once per process.
_indexes_started = False
_indexes_lock = threading.Lock()


def get_db() -> Database:
    """Return the Falcon MongoDB database, connecting on first call.

    The client is memoised at the process level, so the TCP connection pool is
    reused across all requests rather than re-established each call. Safe to
    call from any thread.
    """
    global _db
    if _db is not None:
        return _db
    with _client_lock:
        if _db is None:
            _db = _make_db()
    return _db


def _make_db() -> Database:
    global _client
    uri = os.environ.get("MONGODB_URI", "")
    if not uri or not uri.strip():
        raise ValueError(
            "MONGODB_URI is not set. "
            "Add your MongoDB Atlas connection string to the environment:\n"
            "  MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority"
        )
    client = MongoClient(
        uri,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        # Generous socket timeout — traces and messages can be large.
        # Atlas free/shared tier can be slow under load; 60s avoids spurious
        # timeouts on legitimate reads of growing collections.
        socketTimeoutMS=60000,
        # Bound the pool so a burst of concurrent requests (each turn also fires
        # background audit/extraction/summary tasks) cannot exhaust Atlas.
        maxPoolSize=100,
    )
    _client = client
    # Environment isolation: dev and production share one Atlas cluster but must
    # not share a database. The watcher makes this non-negotiable — it polls the
    # messages collection autonomously, so any instance pointed at the same
    # database will pick up (and execute) commands written by any other. Default
    # is the production name, so deployments need no new configuration; local
    # development sets MONGODB_DB=falcon_dev in backend/.env.
    db_name = os.environ.get("MONGODB_DB", "").strip() or "falcon"
    db = client[db_name]
    logger.info("MongoDB database: %s", db_name)
    # Kick off index creation in the background so the first request is not
    # blocked on ~15 sequential create_index round-trips to Atlas. Indexes only
    # affect query speed, not correctness, and on an existing cluster they
    # already exist, so nothing depends on them being ready before serving.
    _ensure_indexes_async(db)
    return db


def close_db() -> None:
    """Close the shared MongoClient. Called on FastAPI shutdown (lifespan)."""
    global _client, _db
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("closing MongoClient failed: %s", exc)
        _client = None
        _db = None


def get_client() -> MongoClient:
    """Return the shared MongoClient, connecting on first call."""
    get_db()  # ensures _client is initialised
    assert _client is not None  # for type-checkers; get_db() sets it
    return _client


# Transactions require a replica set (Atlas always is one) or a mongos. A plain
# standalone `mongod` used for local dev cannot run them, so we detect support
# once and degrade to non-transactional writes there rather than crashing.
_txn_supported: bool | None = None


def supports_transactions() -> bool:
    """True if the connected deployment can run multi-document transactions."""
    global _txn_supported
    if _txn_supported is None:
        try:
            hello = get_client().admin.command("hello")
            _txn_supported = bool(hello.get("setName") or hello.get("msg") == "isdbgrid")
        except Exception as exc:  # noqa: BLE001 - best effort probe
            logger.warning("transaction-support probe failed, assuming none: %s", exc)
            _txn_supported = False
    return _txn_supported


@contextmanager
def transaction() -> Iterator[ClientSession | None]:
    """Run a block of writes atomically when the deployment supports it.

    Usage::

        with transaction() as s:
            db["a"].delete_many({...}, session=s)
            db["b"].delete_one({...}, session=s)

    Yields a session bound to an open transaction on a replica set / Atlas, so
    the enclosed writes commit all-or-nothing. On a standalone ``mongod`` it
    yields ``None`` and the writes run individually (pymongo accepts
    ``session=None`` as "no session"), so callers use one uniform code path.
    """
    if not supports_transactions():
        yield None
        return
    with get_client().start_session() as session:
        with session.start_transaction():
            yield session


# (collection, keys, options) — created individually so one failure cannot
# cascade. Order is cosmetic; nothing depends on it.
_INDEX_SPECS: list[tuple[str, object, dict]] = [
    ("messages", "identity_id", {}),
    # Compound index so history reads sort by insertion order without a scan
    ("messages", [("identity_id", 1), ("_id", 1)], {}),
    ("traces", "identity_id", {}),
    # Compound index for the user_timestamp lookup used in chat rendering
    ("traces", [("identity_id", 1), ("user_timestamp", 1)], {}),
    ("tokens", "identity_id", {"unique": True}),
    # Audit trail indexes
    ("audit_log", "identity_id", {}),
    ("audit_log", "recorded_at", {}),
    # Memory indexes
    ("memory", "identity_id", {}),
    ("memory", [("identity_id", 1), ("memory_type", 1)], {}),
    ("memory", [("identity_id", 1), ("pinned", -1)], {}),
    # Conversation summary index
    ("conversation_summaries", "identity_id", {"unique": True}),
    # Dual-run log indexes
    ("dual_run_log", "identity_id", {}),
    ("dual_run_log", "recorded_at", {}),
    ("dual_run_log", [("identity_id", 1), ("state_tag", 1)], {}),
    # Identity registry index
    ("identities", "identity_id", {"unique": True}),
    # Categories indexes
    ("categories", "identity_id", {}),
    ("categories", [("identity_id", 1), ("name", 1)], {"unique": True}),
    ("category_messages", "identity_id", {}),
    ("category_messages", "category_id", {}),
    ("category_messages", [("identity_id", 1), ("category_id", 1)], {}),
    ("category_messages", [("identity_id", 1), ("category_id", 1), ("recorded_at", -1)], {}),
    # Admin system indexes
    ("admin_users", "created_at", {}),
    ("portal_users", "created_at", {}),
    ("admin_audit_log", "timestamp", {}),
    # Watcher indexes
    ("watcher_log", "identity_id", {}),
    ("watcher_log", "recorded_at", {}),
    ("watcher_log", [("identity_id", 1), ("recorded_at", -1)], {}),
    # Correctness-critical: the claim mechanism in falcon.watcher._claim()
    ("watcher_processed", "msg_id", {"unique": True}),
    ("watcher_processed", "identity_id", {}),
    ("watcher_settings", "identity_id", {"unique": True}),
]


def _ensure_indexes_async(db: Database) -> None:
    """Create all indexes once per process, in a daemon thread."""
    global _indexes_started
    with _indexes_lock:
        if _indexes_started:
            return
        _indexes_started = True

    def _build() -> None:
        for coll, keys, opts in _INDEX_SPECS:
            # Each index is created independently. They used to share one try
            # block, which meant the first failure silently skipped every index
            # after it — and watcher_processed.msg_id sits near the end of the
            # list. That index is not a query optimisation: _claim() relies on
            # its uniqueness to decide which instance may execute a command, so
            # losing it to an unrelated failure would let every instance run
            # every command.
            try:
                db[coll].create_index(keys, **opts)
            except Exception as exc:  # noqa: BLE001
                level = logger.error if opts.get("unique") else logger.warning
                level("index creation failed on %s%s: %s", coll, keys, exc)

    threading.Thread(target=_build, name="falcon-index-build", daemon=True).start()


def ensure_indexes_now(db: Database) -> None:
    """Create the indexes synchronously. For scripts and one-off setup.

    The normal path is a daemon thread, which a short-lived process kills on
    exit before it finishes — fine for a server, useless for provisioning a new
    database. Call this when the indexes must exist before the process ends.
    """
    for coll, keys, opts in _INDEX_SPECS:
        try:
            db[coll].create_index(keys, **opts)
        except Exception as exc:  # noqa: BLE001
            logger.error("index creation failed on %s%s: %s", coll, keys, exc)
