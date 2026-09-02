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

Two kinds of entry live here
----------------------------
Uploads arrive with a filename and no other metadata. Library entries — written
deliberately through the ``library_store`` command — arrive with a title and
tags instead, because nothing uploaded a file. Both are the same record: ``title``
is the display name and defaults to the filename, so an upload needs no special
casing and a library entry needs no fake filename.
"""
from __future__ import annotations

import hashlib
import logging
import re
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

MAX_TITLE_CHARS = 300
MAX_TAGS = 32
MAX_TAG_CHARS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return get_db()[COLL]


def _new_id() -> str:
    """Short, readable, and unambiguous in chat text."""
    return f"doc_{secrets.token_hex(6)}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def normalize_tags(tags: list[str] | tuple | str | None) -> list[str]:
    """Accept a list or a comma/newline-separated string; return clean tags.

    Casing is preserved — a tag is the user's word, not a slug — but duplicates
    are removed case-insensitively so "Draft" and "draft" cannot both be stored
    and then each fail to match the other.
    """
    if not tags:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[,\n;]+", tags)
    else:
        # A list may still hold comma-joined strings if a caller was sloppy.
        raw = []
        for item in tags:
            raw.extend(re.split(r"[,\n;]+", str(item)))

    out: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        tag = tag.strip().strip("#").strip()
        if not tag:
            continue
        tag = tag[:MAX_TAG_CHARS]
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def display_name(doc: dict) -> str:
    """What to call a stored record: its title, falling back to its filename."""
    return (doc.get("title") or doc.get("filename") or "untitled").strip()


def save(
    identity_id: str,
    filename: str,
    text: str,
    *,
    source: str = "upload",
    kind: str = "document",
    title: str = "",
    tags: list[str] | str | None = None,
    data: bytes | None = None,
    content_type: str = "",
    meta: dict | None = None,
) -> dict[str, Any]:
    """Store a document and verify it is readable. Returns an explicit result.

    Always returns a dict with ``ok``; never raises for storage problems, because
    the caller's job is to report the outcome rather than crash on it.

    ``ok=True``  → ``storage_id`` is real and the content has been read back.
    ``ok=False`` → ``error`` says what went wrong. Nothing was saved that can be
                   relied on, and the caller must say so rather than imply a save.

    Pass ``data`` to keep the original file alongside the extracted text, so the
    upload can be handed back as the file it was rather than as its text. If the
    bytes cannot be stored the text still is: losing the download is worse than
    losing nothing, but far better than discarding a manuscript over it. The
    result then carries ``file_error`` and ``has_file`` stays False.
    """
    text = (text or "").strip()
    filename = (filename or "untitled").strip()
    title = (title or "").strip()[:MAX_TITLE_CHARS] or filename
    tag_list = normalize_tags(tags)

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
        {"storage_id": 1, "filename": 1, "title": 1, "tags": 1, "saved_at": 1,
         "file_id": 1, "content_type": 1, "bytes": 1},
    )
    if existing:
        logger.info(
            "documents_store: %s already stored as %s", filename, existing["storage_id"]
        )
        # Same body, new metadata: keep the id stable (it may already have been
        # quoted back to the user) but do not throw the new title and tags away.
        merged_tags = normalize_tags(list(existing.get("tags") or []) + tag_list)
        changes: dict[str, Any] = {}
        if title and title != existing.get("title"):
            changes["title"] = title
        if merged_tags != list(existing.get("tags") or []):
            changes["tags"] = merged_tags
        # A record stored before the original was kept — or first uploaded as
        # pasted text and now as the actual file — gets its bytes backfilled
        # rather than being left as a text-only entry forever.
        file_error = ""
        if data and not existing.get("file_id"):
            stored = _store_bytes(data, filename, identity_id, content_type,
                                  existing["storage_id"])
            if stored.get("ok"):
                changes.update({
                    "file_id": stored["file_id"],
                    "content_type": stored["content_type"],
                    "bytes": stored["bytes"],
                    "file_sha256": stored["sha256"],
                })
            else:
                file_error = stored.get("error", "")

        if changes:
            try:
                _coll().update_one({"storage_id": existing["storage_id"]}, {"$set": changes})
            except Exception as exc:  # noqa: BLE001
                logger.warning("documents_store: could not update metadata: %s", exc)
                changes = {}

        file_id = changes.get("file_id") or existing.get("file_id", "")
        return {
            "ok": True,
            "storage_id": existing["storage_id"],
            "filename": existing.get("filename", filename),
            "title": changes.get("title") or existing.get("title") or title,
            "tags": changes.get("tags", list(existing.get("tags") or [])),
            "chars": len(text),
            "duplicate": True,
            "metadata_updated": bool(changes),
            "has_file": bool(file_id),
            "file_id": file_id,
            "content_type": changes.get("content_type") or existing.get("content_type", ""),
            "bytes": changes.get("bytes") or existing.get("bytes", 0),
            "file_error": file_error,
            "verified": True,
            "saved_at": existing.get("saved_at"),
            "error": "",
        }

    storage_id = _new_id()

    # The original goes in first. If it fails the text is still stored, but the
    # record must not claim a download it cannot serve, so file_id stays unset.
    file_error = ""
    file_info: dict[str, Any] = {}
    if data:
        stored = _store_bytes(data, filename, identity_id, content_type, storage_id)
        if stored.get("ok"):
            file_info = {
                "file_id": stored["file_id"],
                "content_type": stored["content_type"],
                "bytes": stored["bytes"],
                "file_sha256": stored["sha256"],
            }
        else:
            file_error = stored.get("error", "")
            logger.error(
                "documents_store: text for %r stored but the original was not: %s",
                filename, file_error,
            )

    doc = {
        "storage_id": storage_id,
        "identity_id": identity_id,
        "filename": filename,
        "title": title,
        "tags": tag_list,
        "kind": kind,
        "source": source,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "content_sha256": digest,
        "saved_at": _now(),
        "meta": meta or {},
        **file_info,
    }

    try:
        res = _coll().insert_one(doc)
        if not res.acknowledged:
            _discard_orphan(file_info)
            return {"ok": False, "storage_id": "", "error": "the server did not acknowledge the write"}
    except Exception as exc:  # noqa: BLE001
        logger.error("documents_store: save failed for %r: %s", filename, exc)
        _discard_orphan(file_info)
        return {"ok": False, "storage_id": "", "error": f"{type(exc).__name__}: {exc}"}

    # Verify rather than assume. An id handed back for a document that cannot be
    # read again is worse than an error: it is a false confirmation.
    try:
        back = _coll().find_one({"storage_id": storage_id}, {"content_sha256": 1, "chars": 1})
        if not back:
            _discard_orphan(file_info)
            return {"ok": False, "storage_id": "", "error": "saved document could not be read back"}
        if back.get("content_sha256") != digest:
            _discard_orphan(file_info)
            return {"ok": False, "storage_id": "", "error": "read-back content hash did not match"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "storage_id": "", "error": f"verification read failed: {exc}"}

    _record_write(storage_id, title, identity_id)
    logger.info(
        "documents_store: saved %r as %s (%d chars, source=%s, tags=%s)",
        title, storage_id, len(text), source, tag_list or "none",
    )
    return {
        "ok": True,
        "storage_id": storage_id,
        "filename": filename,
        "title": title,
        "tags": tag_list,
        "chars": len(text),
        "truncated": truncated,
        "duplicate": False,
        "has_file": bool(file_info),
        "file_id": file_info.get("file_id", ""),
        "content_type": file_info.get("content_type", ""),
        "bytes": file_info.get("bytes", 0),
        "file_error": file_error,
        "verified": True,
        "saved_at": doc["saved_at"],
        "error": "",
    }


def _store_bytes(
    data: bytes, filename: str, identity_id: str, content_type: str, storage_id: str
) -> dict[str, Any]:
    """Hand the original off to the file store, converting a crash into a result."""
    try:
        from falcon import file_store

        return file_store.put(
            data,
            filename,
            identity_id=identity_id,
            content_type=content_type,
            storage_id=storage_id,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "file_id": "", "error": f"{type(exc).__name__}: {exc}"}


def _discard_orphan(file_info: dict) -> None:
    """Delete bytes whose record never made it, so they cannot accumulate unreferenced."""
    if not file_info.get("file_id"):
        return
    try:
        from falcon import file_store

        file_store.delete(file_info["file_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("documents_store: orphaned file %s: %s", file_info["file_id"], exc)


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
    """Case-insensitive substring search over title, tags, filename and text.

    A regex scan rather than a text index: these collections are small, and a
    substring match is what someone means by "find my chapter about X" — a text
    index would stem and tokenise and miss exact phrases.

    Tags are matched here too rather than through a separate tag command, so
    "find my drafts" works whether "draft" is a tag or a word in the title.
    """
    query = (query or "").strip()
    if not query:
        return []

    rx = re.compile(re.escape(query), re.I)
    # Mongo applies a regex to each element of an array field, so this matches a
    # record whose *any* tag contains the term.
    q: dict = {"$or": [{"title": rx}, {"tags": rx}, {"filename": rx}, {"text": rx}]}
    if identity_id:
        q["identity_id"] = identity_id
    return list(
        _coll().find(q, {"_id": 0, "text": 0}).sort("saved_at", -1).limit(max(1, min(100, limit)))
    )


def get_file(storage_id: str, identity_id: str = "") -> dict | None:
    """The original uploaded bytes for a stored document, ready to serve.

    Returns None when the document does not exist, was stored as text only, or
    its record points at bytes that are no longer there — the caller must treat
    all three as "no download", never as an empty file.
    """
    doc = get(storage_id, identity_id)
    if not doc or not doc.get("file_id"):
        return None

    from falcon import file_store

    blob = file_store.get(doc["file_id"])
    if not blob:
        logger.error(
            "documents_store: %s points at missing file %s", storage_id, doc["file_id"]
        )
        return None
    # The record's filename is the one the user recognises; GridFS only ever saw
    # whatever was passed at write time.
    blob["filename"] = doc.get("filename") or blob["filename"]
    blob["storage_id"] = storage_id
    return blob


def delete(storage_id: str, identity_id: str = "") -> bool:
    """Remove one document and its original file. The only thing that deletes stored content."""
    q: dict = {"storage_id": (storage_id or "").strip()}
    if identity_id:
        q["identity_id"] = identity_id

    # Read the file id before the record goes, or the bytes become unreachable
    # garbage that nothing points at.
    doomed = _coll().find_one(q, {"file_id": 1})
    res = _coll().delete_one(q)
    if not res.deleted_count:
        return False

    if doomed and doomed.get("file_id"):
        try:
            from falcon import file_store

            file_store.delete(doomed["file_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("documents_store: record deleted but file was not: %s", exc)

    logger.info("documents_store: deleted %s", storage_id)
    return True


def stats(identity_id: str = "") -> dict:
    """Counts for status reporting."""
    q = {"identity_id": identity_id} if identity_id else {}
    coll = _coll()
    total = coll.count_documents(q)
    by_source: dict[str, int] = {}
    for src in coll.distinct("source", q):
        by_source[src] = coll.count_documents({**q, "source": src})
    return {"total": total, "by_source": by_source}
