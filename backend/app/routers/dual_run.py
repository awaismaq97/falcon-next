"""
dual_run.py — Dual-run log reads, aggregate stats, export, delete.

Records are written during the chat send flow (background). Here we expose them
for the Dual Run tab with the aggregate stats the Streamlit tab computed:
total runs, breakthrough count/rate, and a per-state breakdown.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

import falcon.dual_run as DualRun
from falcon.export_utils import make_export_envelope

router = APIRouter(tags=["dual-run"])


def _stats(records: list[dict]) -> dict:
    total = len(records)
    breakthroughs = sum(1 for r in records if r.get("any_breakthrough"))
    per_state: dict[str, dict] = {}
    for r in records:
        tag = r.get("state_tag", "Neutral")
        bucket = per_state.setdefault(tag, {"total": 0, "breakthroughs": 0})
        bucket["total"] += 1
        if r.get("any_breakthrough"):
            bucket["breakthroughs"] += 1
    return {
        "total_runs": total,
        "breakthrough_count": breakthroughs,
        "breakthrough_rate": round(breakthroughs / total, 4) if total else 0.0,
        "per_state": per_state,
    }


@router.get("/dual-run")
def all_dual_runs(limit: int = Query(200, ge=1, le=1000)) -> dict:
    records = DualRun.read_all_records(limit=limit)
    return {"records": records, "stats": _stats(records)}


@router.get("/identities/{identity_id}/dual-run")
def identity_dual_runs(
    identity_id: str, limit: int = Query(200, ge=1, le=1000)
) -> dict:
    records = DualRun.read_records(identity_id, limit=limit)
    return {"records": records, "stats": _stats(records)}


@router.get("/identities/{identity_id}/dual-run/export")
def export_dual_runs(identity_id: str) -> dict:
    records = DualRun.read_records(identity_id, limit=1000)
    return make_export_envelope(identity_id=identity_id, data=records)


@router.delete("/identities/{identity_id}/dual-run")
def delete_dual_runs(identity_id: str) -> dict:
    count = DualRun.delete_records(identity_id)
    return {"deleted_count": count}
