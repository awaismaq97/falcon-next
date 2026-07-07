"""
memory.py — Memory store CRUD, weighted retrieval test, persona editing, export.

Wraps falcon.memory. Persona is stored as one structured string
("Name: …\\nTone: …\\nCommunication style: …\\nCore traits: …") and parsed back
into fields for the editor — same format the Streamlit Memory tab used.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import falcon.config as Config
import falcon.memory as Memory
from app.schemas import (
    MemoryCreateRequest,
    MemoryUpdateRequest,
    PersonaUpdateRequest,
    RetrievalTestRequest,
)
from falcon.db import get_db
from falcon.export_utils import make_export_envelope

router = APIRouter(tags=["memory"])

_PERSONA_KEYS = ["name", "tone", "communication style", "core traits"]


def parse_persona(raw: str) -> dict:
    """Parse the structured persona string into {name, tone, communication_style,
    core_traits}. Multi-line values belong to the field until the next known key.
    """
    fields: dict[str, list[str]] = {k: [] for k in _PERSONA_KEYS}
    current: str | None = None
    for line in (raw or "").splitlines():
        matched = None
        for k in _PERSONA_KEYS:
            if line.lower().startswith(k + ":"):
                matched = k
                break
        if matched is not None:
            current = matched
            first_val = line[len(matched) + 1:].strip()
            if first_val:
                fields[current].append(first_val)
        elif current is not None:
            fields[current].append(line)

    def _join(lines: list[str]) -> str:
        return "\n".join(lines).strip()

    return {
        "name": _join(fields["name"]),
        "tone": _join(fields["tone"]),
        "communication_style": _join(fields["communication style"]),
        "core_traits": _join(fields["core traits"]),
    }


def assemble_persona(f: PersonaUpdateRequest) -> str:
    return (
        f"Name: {f.name}\nTone: {f.tone}\n"
        f"Communication style: {f.communication_style}\nCore traits: {f.core_traits}"
    )


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/memory")
def list_memory(
    identity_id: str,
    memory_type: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=5000),
) -> dict:
    entries = Memory.get_memories(identity_id, memory_type=memory_type, limit=limit)
    return {"identity_id": identity_id, "entries": entries, "count": len(entries)}


@router.post("/identities/{identity_id}/memory", status_code=201)
def add_memory(identity_id: str, req: MemoryCreateRequest) -> dict:
    if not req.content.strip():
        raise HTTPException(400, "Memory content is required.")
    mem_id = Memory.add_memory(
        identity_id=identity_id,
        memory_type=req.memory_type,
        content=req.content,
        tags=req.tags,
        pinned=req.pinned,
        source=req.source,
    )
    return {"_id": mem_id}


@router.patch("/memory/{memory_id}")
def update_memory(memory_id: str, req: MemoryUpdateRequest) -> dict:
    ok = Memory.update_memory(
        memory_id, content=req.content, tags=req.tags, pinned=req.pinned
    )
    if not ok:
        raise HTTPException(404, "Memory entry not found.")
    return {"updated": memory_id}


@router.delete("/memory/{memory_id}")
def delete_memory(memory_id: str) -> dict:
    ok = Memory.delete_memory(memory_id)
    if not ok:
        raise HTTPException(404, "Memory entry not found.")
    return {"deleted": memory_id}


@router.delete("/identities/{identity_id}/memory")
def clear_memory_type(
    identity_id: str,
    memory_type: str = Query(..., description="Memory type to bulk-clear"),
) -> dict:
    """Bulk-clear all entries of one type for an identity. Persona is protected."""
    if memory_type == "persona":
        raise HTTPException(400, "Persona cannot be bulk-cleared; edit it instead.")
    db = get_db()
    result = db["memory"].delete_many(
        {"identity_id": identity_id, "memory_type": memory_type}
    )
    return {"deleted_count": result.deleted_count}


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

@router.get("/identities/{identity_id}/persona")
def get_persona(identity_id: str) -> dict:
    entries = Memory.get_memories(identity_id, memory_type="persona", limit=1)
    if entries:
        return {
            "exists": True,
            "_id": entries[0].get("_id"),
            "fields": parse_persona(entries[0].get("content", "")),
            "raw": entries[0].get("content", ""),
        }
    # None saved yet — pre-fill from config defaults (matches Streamlit).
    return {
        "exists": False,
        "_id": None,
        "fields": parse_persona(Config.default_persona_startup_content),
        "raw": Config.default_persona_startup_content,
    }


@router.put("/identities/{identity_id}/persona")
def save_persona(identity_id: str, req: PersonaUpdateRequest) -> dict:
    content = assemble_persona(req)
    entries = Memory.get_memories(identity_id, memory_type="persona", limit=1)
    if entries:
        Memory.update_memory(entries[0]["_id"], content=content)
        return {"_id": entries[0]["_id"], "raw": content}
    mem_id = Memory.add_memory(
        identity_id=identity_id,
        memory_type="persona",
        content=content,
        source="user",
    )
    return {"_id": mem_id, "raw": content}


# ---------------------------------------------------------------------------
# Retrieval test + export
# ---------------------------------------------------------------------------

@router.post("/identities/{identity_id}/memory/retrieve")
def test_retrieval(identity_id: str, req: RetrievalTestRequest) -> dict:
    result = Memory.retrieve_for_generation(
        identity_id=identity_id,
        query=req.query,
        top_k_per_type=Config.top_k_per_type,
        recency_weight=Config.recency_weight,
        relevance_weight=Config.relevance_weight,
    )
    entries = result.entries
    if not req.use_persona:
        entries = [e for e in entries if e.get("memory_type") != "persona"]
    display = result.to_display_dict()
    display["entries"] = entries
    display["retrieved_count"] = len(entries)
    return display


@router.get("/identities/{identity_id}/memory/export")
def export_memory(identity_id: str) -> dict:
    entries = Memory.get_memories(identity_id, limit=1000)
    return make_export_envelope(identity_id=identity_id, data=entries)
