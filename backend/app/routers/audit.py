"""
audit.py — Inference audit trail.

Two-tier reads matching the Streamlit Audit tab: lightweight summaries for the
list (heavy fields projected out) and full detail on demand, plus a full export.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import falcon.audit as Audit
from falcon.db import get_db
from falcon.export_utils import make_export_envelope

router = APIRouter(tags=["audit"])


@router.get("/audit/summaries")
def all_audit_summaries(limit: int = Query(200, ge=1, le=1000)) -> dict:
    return {"records": Audit.read_all_audit_summaries(limit=limit)}


@router.get("/identities/{identity_id}/audit/summaries")
def identity_audit_summaries(
    identity_id: str,
    limit: int = Query(25, ge=1, le=200),
    skip: int = Query(0, ge=0),
) -> dict:
    records = Audit.read_audit_summaries(identity_id, limit=limit, skip=skip)
    total = Audit.count_audit_records(identity_id)
    return {"records": records, "total": total, "skip": skip, "limit": limit}


@router.get("/audit/{record_id}")
def audit_detail(record_id: str) -> dict:
    doc = Audit.read_audit_detail(record_id)
    if doc is None:
        raise HTTPException(404, "Audit record not found.")
    return doc


@router.get("/identities/{identity_id}/audit/export")
def export_audit(identity_id: str, limit: int = Query(1000, ge=1, le=5000)) -> dict:
    records = Audit.read_audit_records(identity_id, limit=limit)
    return make_export_envelope(identity_id=identity_id, data=records)


@router.delete("/identities/{identity_id}/audit")
def clear_audit(identity_id: str) -> dict:
    db = get_db()
    result = db["audit_log"].delete_many({"identity_id": identity_id})
    return {"deleted_count": result.deleted_count}
