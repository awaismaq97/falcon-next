"""
schemas.py — Pydantic request/response models for the Falcon API.

The backend is stateless: the frontend owns all UI settings (model, toggles,
generation controls) and sends them with each request. This mirrors the exact
inputs the Streamlit session_state fed into _handle_send, so behaviour is
identical while the server holds no per-user session.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryTypeT = Literal["semantic", "episodic", "procedural", "working", "archive", "persona"]
HistoryModeT = Literal["raw", "summary", "hybrid"]


# ---------------------------------------------------------------------------
# Generation + chat settings
# ---------------------------------------------------------------------------

class GenerationSettings(BaseModel):
    temperature: float = 0.0
    top_p: float = 1.0
    repetition_penalty: float = 1.0
    stop_tokens: list[str] = Field(default_factory=list)


class ChatSettings(BaseModel):
    """Every knob the sidebar exposes, sent per-request."""
    model: str
    use_system_prompt: bool = False
    system_prompt_text: str = ""
    use_persona: bool = False
    history_max_turns: int = 15
    history_mode: HistoryModeT = "raw"
    use_tools: bool = True
    use_judge: bool = False
    judge_model: str | None = None
    dual_run_enabled: bool = False
    dual_run_state_tag: str = "Neutral"
    generation: GenerationSettings = Field(default_factory=GenerationSettings)


class DocAttachment(BaseModel):
    """Extracted text from an uploaded document (PDF/Word/Excel/…).

    The frontend uploads the file to /documents/extract, then sends the extracted
    text here. It's injected into the model payload for this turn only; the stored
    user message keeps just a compact 📎 marker.
    """
    filename: str
    text: str


class ChatSendRequest(BaseModel):
    identity_id: str
    message: str = ""
    # Base64 data URLs for image turns (never persisted; routed to vision model).
    images: list[str] = Field(default_factory=list)
    # Extracted document text injected into this turn's payload (not persisted).
    documents: list[DocAttachment] = Field(default_factory=list)
    settings: ChatSettings


class ChatPreviewRequest(BaseModel):
    identity_id: str
    message: str = ""
    documents: list[DocAttachment] = Field(default_factory=list)
    settings: ChatSettings


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

class IdentityCreateRequest(BaseModel):
    identity_id: str


class MessagesSaveRequest(BaseModel):
    entries: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class MemoryCreateRequest(BaseModel):
    memory_type: MemoryTypeT
    content: str
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    source: str = "user"


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    tags: list[str] | None = None
    pinned: bool | None = None


class PersonaUpdateRequest(BaseModel):
    name: str = ""
    tone: str = ""
    communication_style: str = ""
    core_traits: str = ""


class RetrievalTestRequest(BaseModel):
    query: str = ""
    use_persona: bool = True


# ---------------------------------------------------------------------------
# Testing tab
# ---------------------------------------------------------------------------

class TestingRunRequest(BaseModel):
    test_id: str
    variant: str | None = None
    model: str | None = None
