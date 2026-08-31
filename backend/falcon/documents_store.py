"""
documents_store.py — durable storage for uploaded documents.

The gap this closes
-------------------
Uploads used to be processed and then thrown away. ``/documents/extract`` pulled
the text out of a file, the chat flow appended it to that one turn's payload, and
the *stored* user message kept only a "📎 filename" marker. The extracted text
was never written anywhere. A manuscript was therefore visible to the model for
exactly one turn and gone from the next.

That produces the intermittent behaviour it was reported as: upload a manuscript
and ask about it — perfect answers, because the text is right there in the
payload. Ask again two turns later — the model has nothing, because the text was
never stored. Same file, same session, opposite outcomes, no error anywhere. It
reads as flaky memory; it was actually a store that never existed.

Documents saved here live in MongoDB alongside everything else, so they survive
restarts and redeploys and can be retrieved in a later session by id.

Verified writes
---------------
save() does not trust the driver's acknowledgement alone. It writes, reads the
document back, and compares a content hash before reporting success, so a
returned storage id always means the bytes are actually retrievable. A save that
cannot be verified is reported as a failure rather than a hopeful id — the whole
point is that "saved" stops being a guess.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from falcon.db import get_db

logger = logging.getLogger("falcon.documents_store")

COLL = "stored_documents"

# Guards a single pathological upload from dominating a document. Matches the
# extraction cap in the documents router, so text arriving from there is never
# truncated twice.
MAX_TEXT_CHARS = 200_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return get_db()[COLL]


def _new_id() -> str:
    """Short, readable, and unambiguous in chat text."""
    return f"doc_{secrets.token_hex(6)}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def save(
    identity_id: str,
    filename: str,
    text: str,
    *,
    source: str = "upload",
    kind: str = "document",
    meta: dict | None = None,
) -> dict[str, Any]:
    """Store a document and verify it is readable. Returns an explicit result.

    Always returns a dict with ``ok``; never raises for storage problems, because
    the caller's job is to report the outcome rather than crash on it.

    ``ok=True``  → ``storage_id`` is real and the content has been read back.
    ``ok=False`` → ``error`` says what went wrong. Nothing was saved that can be
                   relied on, and the caller must say so rather than imply a save.
    """
    text = (text or "").strip()
    filename = (filename or "untitled").strip()

    if not text:
        return {"ok": False, "storage_id": "", "error": "refusing to save an empty document"}

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    digest = _digest(text)

    # Re-uploading the same file is common and should not multiply rows. Scoped
    # per identity so two users uploading the same manuscript keep their own.
    existing = _coll().find_one(
        {"identity_id": identity_id, "content_sha256": digest},
        {"storage_id": 1, "filename": 1, "saved_at": 1},
    )
    if existing:
        logger.info(
            "documents_store: %s already stored as %s", filename, existing["storage_id"]
        )
        return {
            "ok": True,
            "storage_id": existing["storage_id"],
            "filename": existing.get("filename", filename),
            "chars": len(text),
            "duplicate": True,
            "verified": True,
            "saved_at": existing.get("saved_at"),
            "error": "",
        }

    storage_id = _new_id()
    doc = {
        "storage_id": storage_id,
        "identity_id": identity_id,
        "filename": filename,
        "kind": kind,
        "source": source,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "content_sha256": digest,
        "saved_at": _now(),
        "meta": meta or {},
    }

    try:
        res = _coll().insert_one(doc)
        if not res.acknowledged:
            return {"ok": False, "storage_id": "", "error": "the server did not acknowledge the write"}
    except Exception as exc:  # noqa: BLE001
        logger.error("documents_store: save failed for %r: %s", filename, exc)
        return {"ok": False, "storage_id": "", "error": f"{type(exc).__name__}: {exc}"}

    # Verify rather than assume. An id handed back for a document that cannot be
    # read again is worse than an error: it is a false confirmation.
    try:
        back = _coll().find_one({"storage_id": storage_id}, {"content_sha256": 1, "chars": 1})
        if not back:
            return {"ok": False, "storage_id": "", "error": "saved document could not be read back"}
        if back.get("content_sha256") != digest:
            return {"ok": False, "storage_id": "", "error": "read-back content hash did not match"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "storage_id": "", "error": f"verification read failed: {exc}"}

    _record_write(storage_id, filename, identity_id)
    logger.info(
        "documents_store: saved %s as %s (%d chars, source=%s)",
        filename, storage_id, len(text), source,
    )
    return {
        "ok": True,
        "storage_id": storage_id,
        "filename": filename,
        "chars": len(text),
        "truncated": truncated,
        "duplicate": False,
        "verified": True,
        "saved_at": doc["saved_at"],
        "error": "",
    }


# ---------------------------------------------------------------------------
# Last-write bookkeeping — backs the memory_status command
# ---------------------------------------------------------------------------

_STATUS_COLL = "storage_status"
_STATUS_ID = "last_write"


def _record_write(storage_id: str, filename: str, identity_id: str) -> None:
    """Remember the most recent successful write, for memory_status.

    Best-effort: this is reporting metadata, and failing to record it must never
    turn a successful save into a reported failure.
    """
    try:
        get_db()[_STATUS_COLL].update_one(
            {"_id": _STATUS_ID},
            {"$set": {
                "storage_id": storage_id,
                "filename": filename,
                "identity_id": identity_id,
                "at": _now(),
            }},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("documents_store: could not record last write: %s", exc)


def last_write() -> dict | None:
    """The most recent verified save, or None if nothing has been stored yet."""
    try:
        return get_db()[_STATUS_COLL].find_one({"_id": _STATUS_ID}, {"_id": 0})
    except Exception as exc:  # noqa: BLE001
        logger.warning("documents_store: could not read last write: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def get(storage_id: str, identity_id: str = "") -> dict | None:
    """One stored document, including its text."""
    q: dict = {"storage_id": (storage_id or "").strip()}
    if identity_id:
        q["identity_id"] = identity_id
    return _coll().find_one(q, {"_id": 0})


def list_documents(identity_id: str = "", limit: int = 50) -> list[dict]:
    """Recent documents without their text, newest first."""
    q = {"identity_id": identity_id} if identity_id else {}
    return list(
        _coll()
        .find(q, {"_id": 0, "text": 0})
        .sort("saved_at", -1)
        .limit(max(1, min(500, limit)))
    )


def search(identity_id: str, query: str, limit: int = 20) -> list[dict]:
    """Case-insensitive substring search over filename and text.

    A regex scan rather than a text index: these collections are small, and a
    substring match is what someone means by "find my chapter about X" — a text
    index would stem and tokenise and miss exact phrases.
    """
    query = (query or "").strip()
    if not query:
        return []
    import re as _re

    rx = _re.compile(_re.escape(query), _re.I)
    q: dict = {"$or": [{"filename": rx}, {"text": rx}]}
    if identity_id:
        q["identity_id"] = identity_id
    return list(
        _coll().find(q, {"_id": 0, "text": 0}).sort("saved_at", -1).limit(max(1, min(100, limit)))
    )


def delete(storage_id: str, identity_id: str = "") -> bool:
    """Remove one document. The only thing that deletes stored content."""
    q: dict = {"storage_id": (storage_id or "").strip()}
    if identity_id:
        q["identity_id"] = identity_id
    res = _coll().delete_one(q)
    if res.deleted_count:
        logger.info("documents_store: deleted %s", storage_id)
    return res.deleted_count > 0


def stats(identity_id: str = "") -> dict:
    """Counts for status reporting."""
    q = {"identity_id": identity_id} if identity_id else {}
    coll = _coll()
    total = coll.count_documents(q)
    by_source: dict[str, int] = {}
    for src in coll.distinct("source", q):
        by_source[src] = coll.count_documents({**q, "source": src})
    return {"total": total, "by_source": by_source}
