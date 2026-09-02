"""
file_store.py — the original uploaded file, kept byte-for-byte.

Why this exists separately from documents_store
-----------------------------------------------
documents_store keeps the *text* pulled out of an upload, which is what the model
reads. That text is not the document: a PDF's layout, figures, tables, signatures
and page breaks do not survive extraction, and handing the user back a wall of
extracted text when they asked for their PDF is not the same thing as handing
back their PDF.

So the bytes are stored too, unmodified, and can be downloaded exactly as they
were uploaded. The extracted text and the original file are two views of one
record: documents_store holds the record and the text, this module holds the
bytes, and the record points at them by ``file_id``.

Why GridFS
----------
A single MongoDB document is capped at 16 MB and uploads are allowed up to 25 MB,
so a 20 MB PDF simply cannot be stored inline — the write would fail at the exact
moment someone uploads something big enough to matter. GridFS splits the file
into chunks and reassembles it on read, so the cap stops being a limit we have to
apologise for.

Verified writes, same as everywhere else
----------------------------------------
put() writes, reads the bytes back, and compares length and SHA-256 before
returning a file id. A file id that comes back always means those exact bytes are
retrievable; a write that cannot be proven is reported as a failure rather than
recorded as a success.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from falcon.db import get_db

logger = logging.getLogger("falcon.file_store")

# GridFS creates <root>.files and <root>.chunks alongside the other collections.
BUCKET = "document_files"

# Matches MAX_UPLOAD_BYTES in the documents router. Duplicated deliberately:
# this module is also reachable from the watcher and from scripts, and it must
# refuse an oversized write on its own rather than trusting its callers.
MAX_FILE_BYTES = 25 * 1024 * 1024


def _fs():
    from gridfs import GridFS

    return GridFS(get_db(), collection=BUCKET)


def put(
    data: bytes,
    filename: str,
    *,
    identity_id: str = "",
    content_type: str = "",
    storage_id: str = "",
) -> dict[str, Any]:
    """Store bytes and verify they read back. Never raises for storage problems.

    ``ok=True``  → ``file_id`` is real and the bytes have been read back and
                   compared against the hash of what was handed in.
    ``ok=False`` → ``error`` says what went wrong and no id is returned.
    """
    if not data:
        return {"ok": False, "file_id": "", "error": "refusing to store an empty file"}
    if len(data) > MAX_FILE_BYTES:
        return {
            "ok": False,
            "file_id": "",
            "error": f"file is {len(data):,} bytes, over the {MAX_FILE_BYTES:,} byte limit",
        }

    digest = hashlib.sha256(data).hexdigest()

    try:
        oid = _fs().put(
            data,
            filename=filename or "file",
            contentType=content_type or "application/octet-stream",
            metadata={
                "identity_id": identity_id,
                "storage_id": storage_id,
                "sha256": digest,
                "content_type": content_type or "application/octet-stream",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("file_store: put failed for %r: %s", filename, exc)
        return {"ok": False, "file_id": "", "error": f"{type(exc).__name__}: {exc}"}

    file_id = str(oid)

    # Read the whole thing back. On a chunked store this is the only check that
    # actually proves reassembly works — a successful put() only means the
    # chunks were accepted individually.
    try:
        back = _fs().get(oid).read()
    except Exception as exc:  # noqa: BLE001
        logger.error("file_store: verification read failed for %s: %s", file_id, exc)
        return {"ok": False, "file_id": "", "error": f"stored file could not be read back: {exc}"}

    if len(back) != len(data):
        return {
            "ok": False,
            "file_id": "",
            "error": f"read back {len(back):,} bytes but stored {len(data):,}",
        }
    if hashlib.sha256(back).hexdigest() != digest:
        return {"ok": False, "file_id": "", "error": "read-back bytes did not match the hash"}

    logger.info("file_store: stored %r as %s (%d bytes)", filename, file_id, len(data))
    return {
        "ok": True,
        "file_id": file_id,
        "bytes": len(data),
        "sha256": digest,
        "content_type": content_type or "application/octet-stream",
        "error": "",
    }


def get(file_id: str) -> dict[str, Any] | None:
    """The original bytes plus what is needed to serve them, or None."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId((file_id or "").strip())
    except (InvalidId, TypeError):
        return None

    try:
        grid = _fs().get(oid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("file_store: %s could not be opened: %s", file_id, exc)
        return None

    meta = grid.metadata or {}
    return {
        "file_id": file_id,
        "filename": grid.filename or "file",
        "content_type": meta.get("content_type") or getattr(grid, "content_type", None)
        or "application/octet-stream",
        "bytes": grid.length,
        "sha256": meta.get("sha256", ""),
        "uploaded_at": grid.upload_date,
        "data": grid.read(),
    }


def exists(file_id: str) -> bool:
    """Whether the bytes are still there, without paying to read them."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId((file_id or "").strip())
    except (InvalidId, TypeError):
        return False
    try:
        return _fs().exists(oid)
    except Exception:  # noqa: BLE001
        return False


def delete(file_id: str) -> bool:
    """Remove the stored bytes. Called when its document record is deleted."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId((file_id or "").strip())
    except (InvalidId, TypeError):
        return False
    try:
        _fs().delete(oid)
        logger.info("file_store: deleted %s", file_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("file_store: could not delete %s: %s", file_id, exc)
        return False


def total_bytes() -> int:
    """How much space the originals occupy, for status reporting."""
    try:
        agg = get_db()[f"{BUCKET}.files"].aggregate(
            [{"$group": {"_id": None, "n": {"$sum": "$length"}}}]
        )
        for row in agg:
            return int(row.get("n") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("file_store: could not total bytes: %s", exc)
    return 0
