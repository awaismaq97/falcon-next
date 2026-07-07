"""
config.py — Frontend bootstrap config.

One endpoint the Next.js app calls on load to learn the available models,
defaults, feature flags, and enumerations (memory types, dual-run state tags,
history modes) so nothing is hard-coded on the client and config.yaml stays the
single source of truth.
"""
from __future__ import annotations

from fastapi import APIRouter

import falcon.config as Config

router = APIRouter(tags=["config"])

MEMORY_TYPES = ["semantic", "episodic", "procedural", "working", "archive"]
DUAL_RUN_STATES = ["Neutral", "Focused", "Coherence", "Grief process"]
HISTORY_MODES = ["raw", "summary", "hybrid"]


def _default_persona_fields() -> dict:
    """Parse the config default_persona structured string back into fields."""
    raw = Config.default_persona_startup_content or ""
    fields = {"name": "", "tone": "", "communication_style": "", "core_traits": ""}
    labels = {
        "name": "Name:",
        "tone": "Tone:",
        "communication_style": "Communication style:",
        "core_traits": "Core traits:",
    }
    for line in raw.splitlines():
        for key, label in labels.items():
            if line.startswith(label):
                fields[key] = line[len(label):].strip()
    return fields


@router.get("/config")
def get_config() -> dict:
    return {
        "default_model": Config.default_model,
        "available_models": Config.available_models,
        "default_system_prompt": Config.default_system_prompt,
        "default_persona": _default_persona_fields(),
        "default_persona_identity": Config.default_persona_identity,
        "vision_model": Config.vision_model,
        "extraction_model": Config.extraction_model,
        "summary_model": Config.summary_model,
        "generation": {
            "temperature": Config.generation_temperature,
            "top_p": Config.generation_top_p,
            "repetition_penalty": Config.generation_repetition_penalty,
            "stop_tokens": Config.generation_stop_tokens,
        },
        "history": {
            "max_turns": Config.history_max_turns,
            "modes": HISTORY_MODES,
            "default_mode": "raw",
        },
        "features": {
            "memory_extraction_enabled": Config.memory_extraction_enabled,
            "audit_enabled": Config.audit_enabled,
            # Sidebar defaults matching the Streamlit app's session defaults.
            "default_use_tools": True,
            "default_use_judge": False,
            "default_use_persona": False,
            "default_use_system_prompt": False,
        },
        "retrieval": {
            "top_k_per_type": Config.top_k_per_type,
            "recency_weight": Config.recency_weight,
            "relevance_weight": Config.relevance_weight,
        },
        "assistant_language_patterns": Config.assistant_language_patterns,
        "memory_types": MEMORY_TYPES,
        "dual_run_states": DUAL_RUN_STATES,
    }
