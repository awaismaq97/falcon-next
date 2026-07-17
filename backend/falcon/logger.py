"""
Logger module for Falcon V1.

Provides append_message() — the sole write path for conversation logs.
Messages are stored in the MongoDB 'messages' collection with documents:
  {identity_id, timestamp, role, content}

The collection is ordered by insertion order (natural order), which preserves
chronological sequence exactly as the previous file-based implementation did.

Each append also enforces a fixed retention window (CONVERSATION_RETENTION_TURNS):
only the newest N turns (user message + assistant response) per identity are kept,
and the per-turn artifacts of anything older — context traces and inference audit
records — are cascaded away so the database never accumulates data beyond the
retained window.
"""

from datetime import datetime, timezone

from falcon.db import get_db

# Characters that are forbidden in identity_id values.
_FORBIDDEN_CHARS = {"/", "\\"}
_FORBIDDEN_SEQUENCES = {".."}
_FORBIDDEN_BYTES = {"\x00"}

# Conversation retention cap, counted in TURNS. One turn is a user message plus
# its assistant response, so only the newest CONVERSATION_RETENTION_TURNS user
# messages (and everything after the oldest of them) are kept per identity;
# anything older is deleted on every append. The full retained set is what the
# UI renders.
CONVERSATION_RETENTION_TURNS = 15


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


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string ending with 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_message(identity_id: str, role: str, content: str, timestamp: str = "") -> None:
    """Append one message entry to the MongoDB 'messages' collection.

    Args:
        identity_id: Scoping key for the conversation.
        role: Must be "user" or "assistant".
        content: Message text (may be an empty string).
        timestamp: Optional ISO 8601 timestamp. If omitted, current UTC time is used.

    Raises:
        ValueError: If identity_id contains forbidden characters.
        ValueError: If role is not "user" or "assistant".
    """
    _validate_identity_id(identity_id)

    if role not in ("user", "assistant"):
        raise ValueError(
            f"role must be 'user' or 'assistant', got: {role!r}"
        )

    db = get_db()
    db["messages"].insert_one({
        "identity_id": identity_id,
        "timestamp":   timestamp if timestamp else _utc_now_iso(),
        "role":        role,
        "content":     content,
    })

    enforce_retention(identity_id, keep_turns=CONVERSATION_RETENTION_TURNS)


def enforce_retention(identity_id: str, keep_turns: int = CONVERSATION_RETENTION_TURNS) -> None:
    """Retain only the newest `keep_turns` turns for identity_id, and cascade.

    Called on every append, and also on demand (e.g. when identities are listed)
    so conversations that predate the retention policy — or that haven't been
    messaged since it was introduced — are brought within the window too.

    A turn is a user message plus its assistant response, so the cutoff is the
    keep_turns-th newest *user* message; every message before it (older turns) is
    removed while its paired assistant response is kept. The per-turn artifacts
    that belonged to those dropped messages — context traces and inference audit
    records — are deleted too, so nothing older than the retained window lingers
    anywhere in the database. Traces (`user_timestamp`) and audit records
    (`recorded_at`) are keyed by the same ISO-8601 UTC timestamp format as
    messages, so the oldest retained message's timestamp is a safe delete cutoff.

    A no-op while the conversation still has `keep_turns` or fewer turns. Best
    effort — a prune failure must never fail the write that just succeeded.
    """
    if keep_turns <= 0:
        return
    db = get_db()
    try:
        cutoff = list(
            db["messages"]
            .find(
                {"identity_id": identity_id, "role": "user"},
                {"_id": 1, "timestamp": 1},
            )
            .sort("_id", -1)     # newest user messages first
            .skip(keep_turns - 1)  # land on the oldest user message we still keep
            .limit(1)
        )
        if not cutoff:
            return   # fewer than `keep_turns` turns — nothing to prune
        cutoff_id = cutoff[0]["_id"]
        cutoff_ts = cutoff[0].get("timestamp", "")

        # Drop the messages older than the newest `keep`.
        db["messages"].delete_many(
            {"identity_id": identity_id, "_id": {"$lt": cutoff_id}}
        )

        # Cascade to the artifacts tied to those turns. `$lt cutoff_ts` keeps
        # everything at or after the oldest retained message — audit records are
        # written at turn end (recorded_at ≥ the turn's message timestamps), so a
        # still-retained turn's audit is never caught by this cutoff.
        if cutoff_ts:
            db["traces"].delete_many(
                {"identity_id": identity_id, "user_timestamp": {"$lt": cutoff_ts}}
            )
            db["audit_log"].delete_many(
                {"identity_id": identity_id, "recorded_at": {"$lt": cutoff_ts}}
            )
    except Exception:  # noqa: BLE001 - pruning is best effort, never fatal
        pass
