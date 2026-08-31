"""
documents_backfill.py — recover documents uploaded before durable storage existed.

Uploads were never written to a document store, but they were not entirely lost
either. Every turn's exact model payload is kept in ``audit_log.assembled_payload``
for the audit trail, and the chat flow wrapped attached files in a distinctive
envelope before sending them:

    --- Attached document: manuscript.docx ---
    ...full extracted text...
    --- End of manuscript.docx ---

So the text of any document that was actually sent to the model is still on disk,
inside the audit record for the turn it was sent in. This module walks those
records, pulls the envelopes back out, and saves them into the real store.

What it cannot recover
----------------------
Only documents that reached a model call. A file uploaded but never sent, or one
sent on a turn whose audit record was pruned, left no trace anywhere and is
genuinely gone. Those are reported as unrecoverable rather than quietly omitted,
because "we recovered everything" and "we recovered everything still present"
are different claims and only the second one is true.

Usage:
    python -m falcon.documents_backfill --dry-run     # report, change nothing
    python -m falcon.documents_backfill               # recover for real
"""
from __future__ import annotations

import logging
import re
from typing import Any

from falcon.db import get_db

logger = logging.getLogger("falcon.documents_backfill")

# Matches the envelope written by chat_service._compose_with_documents. The
# filename is captured from the opening line and the closing tag is matched
# loosely, because a name containing regex-special characters would otherwise
# fail to close — losing the document this exists to recover.
_ENVELOPE = re.compile(
    r"--- Attached document:\s*(?P<name>.+?)\s*---\n"
    r"(?P<text>.*?)"
    r"\n--- End of .*?---",
    re.S,
)


def _iter_payload_text(record: dict):
    """Yield every user-authored string in one audit record's payload."""
    for msg in record.get("assembled_payload") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            # Vision turns carry a list of parts; only the text parts matter.
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]


def scan(identity_id: str = "", limit: int = 0) -> list[dict]:
    """Find every recoverable document in the audit log. Reads only.

    Returns one entry per distinct (identity, filename, content) triple, newest
    occurrence first. Deduplicated here as well as in the store, so the dry-run
    count matches what a real run would actually write.
    """
    q: dict = {"identity_id": identity_id} if identity_id else {}
    cursor = (
        get_db()["audit_log"]
        .find(q, {"identity_id": 1, "recorded_at": 1, "assembled_payload": 1})
        .sort("recorded_at", -1)
    )
    if limit:
        cursor = cursor.limit(limit)

    found: dict[tuple, dict] = {}
    scanned = 0
    for record in cursor:
        scanned += 1
        ident = record.get("identity_id", "")
        for text in _iter_payload_text(record):
            if "--- Attached document:" not in text:
                continue  # cheap guard before the expensive regex
            for m in _ENVELOPE.finditer(text):
                name = m.group("name").strip()
                body = m.group("text").strip()
                if not body:
                    continue
                key = (ident, name, hash(body))
                if key not in found:
                    found[key] = {
                        "identity_id": ident,
                        "filename": name,
                        "text": body,
                        "chars": len(body),
                        "seen_at": record.get("recorded_at"),
                    }
    logger.info(
        "documents_backfill: scanned %d audit records, found %d recoverable documents",
        scanned, len(found),
    )
    return sorted(found.values(), key=lambda d: str(d.get("seen_at") or ""), reverse=True)


def run(identity_id: str = "", dry_run: bool = False, limit: int = 0) -> dict[str, Any]:
    """Recover documents into the store. Returns a report of what happened."""
    from falcon import documents_store as Store

    candidates = scan(identity_id=identity_id, limit=limit)
    report: dict[str, Any] = {
        "scanned_candidates": len(candidates),
        "recovered": [],
        "already_present": [],
        "failed": [],
        "dry_run": dry_run,
    }

    for cand in candidates:
        if dry_run:
            report["recovered"].append(
                {"filename": cand["filename"], "chars": cand["chars"],
                 "identity_id": cand["identity_id"], "storage_id": "(dry run)"}
            )
            continue

        res = Store.save(
            cand["identity_id"],
            cand["filename"],
            cand["text"],
            source="backfill",
            meta={"recovered_from": "audit_log", "originally_seen_at": cand.get("seen_at")},
        )
        entry = {
            "filename": cand["filename"],
            "chars": cand["chars"],
            "identity_id": cand["identity_id"],
            "storage_id": res.get("storage_id", ""),
        }
        if not res["ok"]:
            entry["error"] = res["error"]
            report["failed"].append(entry)
        elif res.get("duplicate"):
            report["already_present"].append(entry)
        else:
            report["recovered"].append(entry)

    logger.info(
        "documents_backfill: %d recovered, %d already present, %d failed%s",
        len(report["recovered"]), len(report["already_present"]),
        len(report["failed"]), " (dry run)" if dry_run else "",
    )
    return report


def format_report(report: dict) -> str:
    """Human-readable summary, explicit about what could not be recovered."""
    lines = []
    mode = " (DRY RUN — nothing was written)" if report["dry_run"] else ""
    lines.append(f"DOCUMENT BACKFILL{mode}")
    lines.append("")
    lines.append(f"Recoverable documents found : {report['scanned_candidates']}")
    lines.append(f"Recovered into storage      : {len(report['recovered'])}")
    lines.append(f"Already stored              : {len(report['already_present'])}")
    lines.append(f"Failed to save              : {len(report['failed'])}")

    for entry in report["recovered"][:40]:
        lines.append(f"  [SAVED]  {entry['storage_id']}  {entry['filename']} ({entry['chars']} chars)")
    for entry in report["failed"][:20]:
        lines.append(f"  [FAILED] {entry['filename']} — {entry.get('error', '')}")

    lines.append("")
    if report["scanned_candidates"] == 0:
        lines.append(
            "No recoverable documents were found in the audit log. Any document uploaded "
            "before durable storage existed and not present here is UNSAVED and cannot be "
            "recovered — it was never written to disk in any form."
        )
    else:
        lines.append(
            "Only documents that reached a model call are recoverable. Anything uploaded "
            "but never sent, or sent on a turn whose audit record has since been pruned, "
            "is UNSAVED and unrecoverable."
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Recover pre-storage document uploads from the audit log.")
    ap.add_argument("--dry-run", action="store_true", help="report what would be recovered, write nothing")
    ap.add_argument("--identity", default="", help="restrict to one identity_id")
    ap.add_argument("--limit", type=int, default=0, help="only scan the N most recent audit records")
    args = ap.parse_args()

    print(format_report(run(identity_id=args.identity, dry_run=args.dry_run, limit=args.limit)))
