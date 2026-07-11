"""
identities.py — Identity lifecycle, history, token totals, message editing.

Ports the identity + token + log-editor behaviour from the Streamlit app:
  - list identities (registry ∪ messages) with message counts (one aggregation)
  - create (persist + seed default persona)
  - delete (cascade every collection + registry entry)
  - load history (paged, newest-window)
  - clear conversation (keep persona)
  - token totals get
  - safe full-rewrite of an identity's messages (insert-then-delete ordering)

Handlers are plain ``def`` so FastAPI runs the blocking pymongo calls in its
threadpool, keeping the event loop free.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

import falcon.config as Config
import falcon.identity as Identity
import falcon.memory as Memory
from app.schemas import (
    IdentityCreateRequest,
    MessagesSaveRequest,
    SystemPromptSaveRequest,
)
from falcon.db import get_db, transaction

router = APIRouter(tags=["identities"])


def _message_counts() -> dict[str, int]:
    """Return {identity_id: message_count} in a single aggregation round-trip."""
    db = get_db()
    pipeline = [{"$group": {"_id": "$identity_id", "n": {"$sum": 1}}}]
    return {doc["_id"]: doc["n"] for doc in db["messages"].aggregate(pipeline)}


@router.get("/identities")
def list_identities() -> dict:
    ids = Identity.list_identities()
    # Ensure the always-present default identity is included.
    id_set = set(ids) | {"default"}
    counts = _message_counts()
    items = [
        {"identity_id": iid, "message_count": counts.get(iid, 0)}
        for iid in sorted(id_set)
    ]
    return {"identities": items, "default": "default"}


@router.post("/identities", status_code=201)
def create_identity(req: IdentityCreateRequest) -> dict:
    new_id = req.identity_id.strip()
    if not new_id:
        raise HTTPException(400, "Identity name is required.")
    if "." in new_id:
        raise HTTPException(400, "Name cannot contain a dot.")
    # Identity.create_identity validates path-traversal chars and raises ValueError
    # (handled globally → 400).
    Identity.create_identity(new_id)
    # Seed the default persona for the new identity (matches Streamlit create).
    if Config.default_persona_startup_content:
        existing = get_db()["memory"].find_one(
            {"identity_id": new_id, "memory_type": "persona"}
        )
        if not existing:
            Memory.add_memory(
                identity_id=new_id,
                memory_type="persona",
                content=Config.default_persona_startup_content,
                source="user",
            )
    return {"identity_id": new_id, "message_count": 0}


@router.delete("/identities/{identity_id}")
def delete_identity(identity_id: str) -> dict:
    if identity_id == "default":
        raise HTTPException(400, "The 'default' identity cannot be deleted.")
    Identity._validate_identity_id(identity_id)  # raises ValueError → 400
    db = get_db()
    # Cascade delete across every collection scoped by identity_id, atomically:
    # a mid-cascade failure would otherwise orphan memory/traces/audit under a
    # now-deleted identity. On a replica set / Atlas this all-or-nothing commits;
    # on standalone mongod it degrades to sequential deletes (session=None).
    with transaction() as s:
        for coll in ("messages", "memory", "traces", "audit_log", "dual_run_log"):
            db[coll].delete_many({"identity_id": identity_id}, session=s)
        db["tokens"].delete_one({"identity_id": identity_id}, session=s)
        db["conversation_summaries"].delete_one({"identity_id": identity_id}, session=s)
        db["identities"].delete_one({"identity_id": identity_id}, session=s)
    return {"deleted": identity_id}


@router.get("/identities/{identity_id}/history")
def load_history(
    identity_id: str,
    limit: int = Query(2000, ge=1, le=5000),
) -> dict:
    Identity._validate_identity_id(identity_id)
    history = Identity.load_history(identity_id, limit=limit)
    return {"identity_id": identity_id, "messages": history, "count": len(history)}


@router.post("/identities/{identity_id}/clear")
def clear_conversation(identity_id: str) -> dict:
    """Clear conversation, traces, tokens, audit, non-persona memory, summary."""
    Identity._validate_identity_id(identity_id)
    db = get_db()
    # Atomic so a partial failure can't leave, e.g., traces without their messages.
    with transaction() as s:
        db["messages"].delete_many({"identity_id": identity_id}, session=s)
        db["traces"].delete_many({"identity_id": identity_id}, session=s)
        db["tokens"].delete_one({"identity_id": identity_id}, session=s)
        db["audit_log"].delete_many({"identity_id": identity_id}, session=s)
        db["memory"].delete_many(
            {"identity_id": identity_id, "memory_type": {"$ne": "persona"}}, session=s
        )
        db["conversation_summaries"].delete_one({"identity_id": identity_id}, session=s)
    return {"cleared": identity_id}


@router.get("/identities/{identity_id}/system-prompt")
def get_system_prompt(identity_id: str) -> dict:
    """Return this identity's saved system prompt, or the config default.

    `exists` is False when nothing has been saved yet for this identity — the
    frontend then shows the global default until the user edits it.
    """
    Identity._validate_identity_id(identity_id)
    stored = Identity.get_system_prompt(identity_id)
    if stored is not None:
        return {
            "exists": True,
            "system_prompt": stored["system_prompt"],
            "use_system_prompt": stored["use_system_prompt"],
        }
    return {
        "exists": False,
        "system_prompt": Config.default_system_prompt,
        "use_system_prompt": False,
    }


@router.put("/identities/{identity_id}/system-prompt")
def save_system_prompt(identity_id: str, req: SystemPromptSaveRequest) -> dict:
    """Persist this identity's system prompt + toggle to the database."""
    Identity._validate_identity_id(identity_id)
    Identity.set_system_prompt(identity_id, req.system_prompt, req.use_system_prompt)
    return {
        "identity_id": identity_id,
        "exists": True,
        "system_prompt": req.system_prompt,
        "use_system_prompt": req.use_system_prompt,
    }


@router.get("/identities/{identity_id}/tokens")
def get_tokens(identity_id: str) -> dict:
    db = get_db()
    doc = db["tokens"].find_one({"identity_id": identity_id}, {"_id": 0})
    if not doc:
        return {"prompt": 0, "completion": 0, "total": 0}
    return {
        "prompt": doc.get("prompt", 0),
        "completion": doc.get("completion", 0),
        "total": doc.get("total", 0),
    }


@router.put("/identities/{identity_id}/messages")
def save_messages(identity_id: str, req: MessagesSaveRequest) -> dict:
    """Full rewrite of an identity's messages (Logs editor + single delete).

    Insert-then-delete ordering so a mid-operation failure cannot destroy the
    conversation: write the new copy tagged with a batch marker FIRST, only then
    delete everything not in the batch, then strip the marker.
    """
    Identity._validate_identity_id(identity_id)
    db = get_db()
    batch = uuid.uuid4().hex
    new_docs = [
        {
            "identity_id": identity_id,
            "timestamp": e.get("timestamp", ""),
            "role": e.get("role", "user"),
            "content": e.get("content", ""),
            "_rewrite_batch": batch,
        }
        for e in req.entries
    ]
    # On a replica set / Atlas the whole rewrite is one atomic commit. The
    # insert-then-delete batch ordering is kept so that even on standalone
    # mongod (no transaction) a mid-operation failure can't destroy the
    # conversation — at worst it leaves recoverable duplicates, never data loss.
    with transaction() as s:
        if new_docs:
            db["messages"].insert_many(new_docs, session=s)
        db["messages"].delete_many(
            {"identity_id": identity_id, "_rewrite_batch": {"$ne": batch}}, session=s
        )
        db["messages"].update_many(
            {"identity_id": identity_id, "_rewrite_batch": batch},
            {"$unset": {"_rewrite_batch": ""}},
            session=s,
        )
    return {"identity_id": identity_id, "count": len(new_docs)}
