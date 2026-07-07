"""
Identity Manager module for Falcon V1.

Provides operations over per-identity data:
  - list_identities()   — enumerate all identities (from the identities collection + messages)
  - create_identity()   — persist a new identity immediately (before any messages)
  - load_history()      — load the message history for a given identity
  - clear_identity()    — delete all messages for a given identity

Data lives in:
  - MongoDB 'identities' collection  — one doc per identity, written on creation
  - MongoDB 'messages'   collection  — conversation history, scoped by identity_id
"""

import time
from datetime import datetime, timezone

from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError

from falcon.db import get_db

# Transient MongoDB/Atlas errors that are safe to retry — a brief network blip
# or shared-tier server selection timeout should not surface as "history gone".
_TRANSIENT_DB_ERRORS = (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError)

# Forbidden characters / sequences in identity_id values.
_FORBIDDEN_CHARS = ("/", "\\")
_FORBIDDEN_SEQUENCES = ("..",)
_FORBIDDEN_BYTES = ("\x00",)


def _validate_identity_id(identity_id: str) -> None:
    """Raise ValueError if identity_id contains path-traversal characters.

        - forward slash  /
        - backslash      \\
        - double-dot     ..
        - null byte      \\x00
    """
    bad: list[str] = []

    if "/" in identity_id:
        bad.append("/")
    if "\\" in identity_id:
        bad.append("\\")
    if ".." in identity_id:
        bad.append("..")
    if "\x00" in identity_id:
        bad.append("null byte")

    if bad:
        raise ValueError(
            f"identity_id contains disallowed character(s): {', '.join(bad)}"
        )


def list_identities() -> list[str]:
    """Return all identity IDs — from the identities collection union messages.

    This ensures identities created before their first message are included.
    Returns an empty list if no identities exist at all.
    Each ID appears exactly once.
    """
    db = get_db()
    from_registry = set(
        doc["identity_id"]
        for doc in db["identities"].find({}, {"identity_id": 1, "_id": 0})
    )
    from_messages = set(db["messages"].distinct("identity_id"))
    return sorted(from_registry | from_messages)


def create_identity(identity_id: str) -> None:
    """Persist a new identity immediately, before any messages are sent.

    Inserts a document into the 'identities' collection so the identity
    shows up in list_identities() right away without needing a first message.
    No-op if the identity already exists.

    Args:
        identity_id: The new identity name.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db  = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db["identities"].update_one(
        {"identity_id": identity_id},
        {"$setOnInsert": {"identity_id": identity_id, "created_at": now}},
        upsert=True,
    )


def load_history(identity_id: str, limit: int = 2000) -> list[dict]:
    """Return the message history for identity_id in chronological order.

    Behaviour:
    - Validates identity_id for path-traversal characters.
    - Returns an empty list [] if no messages exist for this identity.
    - Returns the NEWEST `limit` messages, in chronological order. The read
      sorts by _id descending (index-backed via the compound (identity_id, _id)
      index in db.py), takes `limit`, then reverses back to oldest→newest.
    - Capped at `limit` to prevent unbounded reads on large histories.
    - Each entry is a plain dict with keys: timestamp, role, content.
      The MongoDB _id field is stripped so the shape is identical to the
      old JSON format callers expect.

    Loading the newest (not the oldest) `limit` messages is essential: with an
    ascending sort + cap, once a conversation grew past `limit` the query kept
    returning the first `limit` docs, so every newly-sent message fell outside
    the window and vanished from the UI on the next reload — even though it was
    safely written to MongoDB. Reversing that makes recent messages always load.

    Args:
        identity_id: The identity whose history to load.
        limit: Maximum number of (most recent) messages to return (default 2000).

    Returns:
        A list of {timestamp, role, content} dicts in chronological order.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db = get_db()

    # Retry transient read failures with brief backoff before giving up, so a
    # single Atlas blip does not blank the conversation in the UI. The read is
    # idempotent, so retrying is always safe.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            # Newest-first so the cap keeps the most recent `limit` messages,
            # then reverse to hand callers chronological (oldest→newest) order.
            cursor = (
                db["messages"]
                .find(
                    {"identity_id": identity_id},
                    {"_id": 0, "identity_id": 0},   # strip internal fields
                )
                .sort("_id", -1)   # newest first, uses (identity_id, _id) index
                .limit(limit)
            )
            docs = list(cursor)
            docs.reverse()   # back to chronological order for the caller
            return docs
        except _TRANSIENT_DB_ERRORS as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))   # 0.5s, then 1.0s

    # Exhausted retries — re-raise so the caller can show a transient error and
    # retry on the next rerun (it must NOT cache this as an empty history).
    raise last_exc


def clear_identity(identity_id: str) -> None:
    """Delete all messages for identity_id from MongoDB.

    Behaviour:
    - Validates identity_id for path-traversal characters.
    - Deletes all documents where identity_id matches.
    - No-op if no documents exist — does not raise.
    - Does NOT affect any other identity's messages.

    Args:
        identity_id: The identity whose messages should be deleted.

    Raises:
        ValueError: If identity_id contains forbidden characters.
    """
    _validate_identity_id(identity_id)

    db = get_db()
    db["messages"].delete_many({"identity_id": identity_id})
