"""
traces.py — Per-turn reasoning traces + context snapshots.

Backs three UI surfaces:
  - Chat tab: a lightweight index of which turns have a trace (⌥ context button),
    plus on-demand fetch of one turn's assembled payload / steps.
  - Context tab: the most recent turn's full context snapshot.
  - Logs tab: full trace documents with per-stage steps.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from falcon.db import get_db

router = APIRouter(tags=["traces"])


@router.get("/identities/{identity_id}/trace-index")
def trace_index(identity_id: str) -> dict:
    """Return the set of user_timestamps that have a trace (timestamps only)."""
    db = get_db()
    cursor = (
        db["traces"]
        .find({"identity_id": identity_id}, {"_id": 0, "user_timestamp": 1})
        .limit(1000)
    )
    return {"timestamps": [doc["user_timestamp"] for doc in cursor if doc.get("user_timestamp")]}


@router.get("/identities/{identity_id}/traces")
def list_traces(
    identity_id: str,
    limit: int = Query(25, ge=1, le=200),
    skip: int = Query(0, ge=0),
) -> dict:
    db = get_db()
    cursor = (
        db["traces"]
        .find({"identity_id": identity_id}, {"_id": 0, "identity_id": 0})
        .sort("user_timestamp", -1)
        .skip(skip)
        .limit(limit)
    )
    traces = list(cursor)
    total = db["traces"].count_documents({"identity_id": identity_id})
    return {"traces": traces, "total": total, "skip": skip, "limit": limit}


@router.get("/identities/{identity_id}/context/latest")
def latest_context(identity_id: str) -> dict:
    """Most recent turn's context snapshot for the Context tab."""
    db = get_db()
    doc = db["traces"].find_one(
        {"identity_id": identity_id},
        {"_id": 0, "context_snapshot": 1, "user_timestamp": 1, "user": 1},
        sort=[("user_timestamp", -1)],
    )
    if not doc:
        return {"exists": False, "context_snapshot": None}
    return {
        "exists": True,
        "user_timestamp": doc.get("user_timestamp"),
        "user": doc.get("user"),
        "context_snapshot": doc.get("context_snapshot"),
    }


@router.get("/identities/{identity_id}/traces/{user_ts}")
def get_trace(identity_id: str, user_ts: str) -> dict:
    db = get_db()
    doc = db["traces"].find_one(
        {"identity_id": identity_id, "user_timestamp": user_ts},
        {"_id": 0, "identity_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Trace not found.")
    return doc


@router.get("/identities/{identity_id}/traces/{user_ts}/payload")
def get_trace_payload(identity_id: str, user_ts: str) -> dict:
    """The exact assembled payload sent to the model for this turn."""
    db = get_db()
    doc = db["traces"].find_one(
        {"identity_id": identity_id, "user_timestamp": user_ts},
        {"_id": 0, "steps": 1},
    )
    if not doc:
        raise HTTPException(404, "Trace not found.")
    for step in doc.get("steps", []):
        if step.get("stage") == "payload built":
            data = step.get("data", {})
            if isinstance(data, dict):
                return {"payload": data.get("payload")}
    return {"payload": None}


@router.delete("/identities/{identity_id}/traces/{user_ts}")
def delete_trace(identity_id: str, user_ts: str) -> dict:
    db = get_db()
    db["traces"].delete_one({"identity_id": identity_id, "user_timestamp": user_ts})
    return {"deleted": user_ts}
