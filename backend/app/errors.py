"""
errors.py — Turn raw OpenRouter/OpenAI exceptions into actionable messages.

Ported verbatim in spirit from the Streamlit app's _friendly_api_error so the
UX around rate-limits / credit / policy blocks is preserved: the client is told
to wait and retry rather than shown a raw stack-trace string.
"""
from __future__ import annotations


def friendly_api_error(exc: Exception) -> str:
    s = str(exc)
    code = (
        getattr(exc, "status_code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )

    def _hit(target: int, *needles: str) -> bool:
        return code == target or str(target) in s or any(n in s.lower() for n in needles)

    if _hit(429, "rate limit", "rate-limit", "too many requests"):
        return (
            "Provider is rate-limiting requests (429). Wait ~30–60s and retry. "
            "Note: Tools makes 2–3 API calls per turn and background memory tasks "
            "add more — turning Tools/Judge off reduces calls if this keeps happening."
        )
    if _hit(402, "insufficient", "credit"):
        return "OpenRouter reports insufficient credits (402). Add credits or switch to a free model."
    if _hit(403, "forbidden", "moderat"):
        return (
            "Provider refused the request (403). Usually a temporary rate/policy block — "
            "wait and retry, or switch to a different model in the sidebar."
        )
    if _hit(423, "locked"):
        return "Resource temporarily locked (423) — a transient provider issue. Wait and retry."
    if _hit(401, "unauthor", "api key", "invalid key"):
        return "Authentication failed (401). Check OPENROUTER_API_KEY in the environment."
    return f"Inference failed: {s}"
