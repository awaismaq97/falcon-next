"""
memory_bridge.py — a real read/write probe against the live database.

Why this exists
---------------
"Memory works" is a claim that is easy to make and, until now, impossible to
check. The watcher persona used to live in config.yaml on an ephemeral
container filesystem, so it was silently reset on every redeploy while the model
went on describing capabilities it no longer had. Nothing in the system could
tell the difference between storage that worked and storage that had quietly
stopped, so the failure surfaced as the assistant confidently claiming to
remember things it had lost.

This module makes that claim falsifiable. It performs an actual round-trip —
write, read back, compare, update, re-read — against the same database and the
same connection the rest of the application uses, and reports exactly which step
succeeded and how long each took.

Same-call round-trips are not enough
------------------------------------
Writing a document and reading it back one millisecond later proves the
connection is up. It does not prove *persistence*, which is the word in the
name and the thing that actually broke before: an in-memory store, a stale
cache, or a container-local file would all pass a same-call round-trip and still
lose everything on restart.

So each probe is kept rather than deleted, stamped with the id of the process
that wrote it. The durability check then looks for probes written by *earlier
processes*. Finding them is positive evidence that data survived a restart —
the only evidence that means anything here. On a genuinely fresh database there
are none yet, and the report says exactly that rather than implying failure.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from falcon.db import get_db

logger = logging.getLogger("falcon.memory_bridge")

COLL = "memory_bridge_probes"

# Identifies the process that wrote a probe. Generated at import, so every
# restart, redeploy and worker gets a distinct value — which is precisely what
# lets the durability check tell "written by an earlier process" apart from
# "written moments ago by me".
BOOT_ID = uuid.uuid4().hex[:12]
BOOT_STARTED = datetime.now(timezone.utc)

# Probes are evidence, not garbage, so they are kept — but not forever. Enough
# history to demonstrate persistence across several restarts, few enough that
# the collection stays negligible.
_KEEP_PROBES = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return get_db()[COLL]


class _Steps:
    """Records each stage with its own timing, so a failure names the exact step.

    A single try/except around the whole round-trip would report "it broke"
    without saying whether the write, the read or the comparison broke — which
    are three different faults with three different fixes.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []

    def run(self, name: str, fn) -> Any:
        started = time.perf_counter()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 — the point is to report, not raise
            self.items.append({
                "step": name, "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "ms": round((time.perf_counter() - started) * 1000, 1),
            })
            raise
        detail, result = value if isinstance(value, tuple) else ("", value)
        self.items.append({
            "step": name, "ok": True, "detail": detail,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return result


class BridgeFailure(Exception):
    """A step completed without raising but produced the wrong answer."""


def check() -> dict:
    """Run the full probe. Never raises — failure is data, not an exception.

    Returns a dict with ``ok``, per-step timings, and a ``durability`` block.
    Callers must report what this says rather than assuming success; that
    assumption is the bug this module exists to catch.
    """
    steps = _Steps()
    nonce = uuid.uuid4().hex
    payload = f"falcon-memory-bridge/{nonce}"
    started_at = _now()

    result: dict[str, Any] = {
        "ok": False,
        "checked_at": started_at,
        "boot_id": BOOT_ID,
        "database": os.environ.get("MONGODB_DB", "").strip() or "falcon",
        "collection": COLL,
        "steps": steps.items,
        "durability": {},
        "failed_step": "",
        "error": "",
    }

    try:
        # 1. Connection — a real server round-trip, not just a handle.
        def _connect():
            from falcon.db import get_client

            hello = get_client().admin.command("hello")
            kind = hello.get("setName") or ("mongos" if hello.get("msg") == "isdbgrid" else "standalone")
            return f"reachable, {kind}", hello

        steps.run("connect", _connect)

        # 2. Write.
        def _write():
            doc = {
                "nonce": nonce,
                "payload": payload,
                "boot_id": BOOT_ID,
                "written_at": started_at,
                "updated": False,
            }
            res = _coll().insert_one(doc)
            if not res.acknowledged:
                raise BridgeFailure("the server did not acknowledge the write")
            return f"acknowledged, _id {res.inserted_id}", res.inserted_id

        inserted_id = steps.run("write", _write)

        # 3. Read back and compare. Queried by nonce rather than by _id so this
        #    exercises an actual query rather than a primary-key fetch.
        def _read_back():
            found = _coll().find_one({"nonce": nonce})
            if found is None:
                raise BridgeFailure("the document just written could not be read back")
            if found.get("payload") != payload:
                raise BridgeFailure(
                    f"read-back mismatch: wrote {payload!r}, read {found.get('payload')!r}"
                )
            return "payload matches exactly", found

        steps.run("read_back", _read_back)

        # 4. Update and re-read. Insert-only storage can look healthy while
        #    updates silently fail, and updates are what memory edits rely on.
        def _update():
            new_payload = f"{payload}/updated"
            res = _coll().update_one(
                {"nonce": nonce},
                {"$set": {"payload": new_payload, "updated": True, "updated_at": _now()}},
            )
            if res.matched_count != 1:
                raise BridgeFailure(f"update matched {res.matched_count} documents, expected 1")
            again = _coll().find_one({"nonce": nonce})
            if not again or again.get("payload") != new_payload:
                raise BridgeFailure("the update did not survive a re-read")
            return "update applied and confirmed by re-read", True

        steps.run("update", _update)

        # 5. Durability — the part that actually justifies the word "persistent".
        def _durability():
            coll = _coll()
            earlier = coll.count_documents({"boot_id": {"$ne": BOOT_ID}})
            boots = len(coll.distinct("boot_id", {"boot_id": {"$ne": BOOT_ID}}))
            oldest_doc = coll.find_one(
                {"boot_id": {"$ne": BOOT_ID}}, sort=[("written_at", 1)]
            )
            oldest = oldest_doc.get("written_at") if oldest_doc else None
            result["durability"] = {
                "probes_from_earlier_processes": earlier,
                "earlier_boots": boots,
                "oldest_probe_at": oldest,
                # The distinction the whole module turns on: proven, versus
                # simply not yet demonstrable on a database this new.
                "survived_restart": earlier > 0,
            }
            if earlier:
                return f"{earlier} probe(s) from {boots} earlier process(es)", True
            return "no earlier probes yet — this is the first process to write one", True

        steps.run("durability", _durability)

        # 6. Prune. Deliberately last: pruning is housekeeping, and a failure
        #    here says nothing about whether reads and writes work, so it must
        #    not be able to fail the check.
        try:
            coll = _coll()
            keep = [d["_id"] for d in coll.find({}, {"_id": 1})
                    .sort("written_at", -1).limit(_KEEP_PROBES)]
            if keep:
                coll.delete_many({"_id": {"$nin": keep}})
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_bridge: probe pruning skipped: %s", exc)

        result["ok"] = True
        result["probe_id"] = str(inserted_id)
        logger.info(
            "memory_bridge: OK — db=%s round-trip in %.1fms, survived_restart=%s",
            result["database"],
            sum(s["ms"] for s in steps.items),
            result["durability"].get("survived_restart"),
        )

    except Exception as exc:  # noqa: BLE001
        failed = steps.items[-1]["step"] if steps.items else "connect"
        result["failed_step"] = failed
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.error("memory_bridge: FAILED at %r — %s", failed, result["error"])

    result["total_ms"] = round(sum(s["ms"] for s in steps.items), 1)
    return result


def format_report(res: dict) -> str:
    """Render a check() result as text for the chat, stating the verdict plainly."""
    # Rendered as markdown in the chat, so consecutive plain lines would collapse
    # into one paragraph. Bullets and a table keep the report readable there and
    # still legible as plain text anywhere else.
    lines: list[str] = []
    verdict = "OPERATIONAL" if res["ok"] else f"FAILED at step '{res['failed_step']}'"
    lines.append(f"MEMORY BRIDGE: {verdict}")
    lines.append("")
    lines.append(f"- **Database:** {res['database']}")
    lines.append(f"- **Collection:** {res['collection']}")
    lines.append(f"- **Process:** {res['boot_id']}")
    lines.append(f"- **Total:** {res.get('total_ms', 0)} ms")
    lines.append("")
    lines.append("| Step | Result | Time | Detail |")
    lines.append("| --- | --- | --- | --- |")

    for step in res["steps"]:
        mark = "OK" if step["ok"] else "**FAIL**"
        detail = (step["detail"] or "—").replace("|", "\\|")
        lines.append(f"| {step['step']} | {mark} | {step['ms']} ms | {detail} |")

    if not res["ok"]:
        lines.append("")
        lines.append(f"**Error:** {res['error']}")
        lines.append("")
        lines.append(
            "Storage is NOT confirmed working. Do not claim anything was remembered, "
            "saved or recalled until this reports OPERATIONAL."
        )
        return "\n".join(lines)

    dur = res.get("durability") or {}
    lines.append("")
    if dur.get("survived_restart"):
        oldest = dur.get("oldest_probe_at")
        when = oldest.strftime("%Y-%m-%d %H:%M UTC") if hasattr(oldest, "strftime") else str(oldest)
        lines.append(
            f"**PERSISTENCE CONFIRMED** — {dur['probes_from_earlier_processes']} probe(s) written by "
            f"{dur['earlier_boots']} earlier process(es) are still readable, the oldest from "
            f"{when}. Data survives restarts and redeploys, not merely this session."
        )
    else:
        lines.append(
            "**READ/WRITE CONFIRMED, PERSISTENCE NOT YET PROVEN** — this is the first process to "
            "write a probe, so there is nothing from an earlier run to verify against. Run this "
            "again after a restart and it will report on durability properly."
        )
    return "\n".join(lines)


def startup_report() -> str:
    """One-line summary for the boot log."""
    res = check()
    if not res["ok"]:
        return (
            f"MEMORY BRIDGE FAILED at '{res['failed_step']}' — {res['error']} "
            f"(db={res['database']}). Persistence is NOT working."
        )
    dur = res.get("durability") or {}
    proof = (
        f"persistence confirmed across {dur.get('earlier_boots', 0)} earlier process(es)"
        if dur.get("survived_restart")
        else "first probe on this database — durability not yet demonstrable"
    )
    return (
        f"MEMORY BRIDGE OK — db={res['database']} round-trip {res['total_ms']}ms, {proof}"
    )
