"""
errors.py — Turn raw OpenRouter/OpenAI exceptions into actionable messages.

Classification is driven by the real HTTP status code when the SDK exposes one
(openai.APIStatusError and subclasses set .status_code). Text matching is only a
fallback for when the code is absent — and it matches on descriptive phrases
("rate limit"), never on the bare number, because a bare "429"/"402" routinely
appears inside ids, token counts and timestamps and would misclassify unrelated
errors. The underlying provider message is appended so a 429/5xx is never opaque
— on a paid key the real reason (which upstream provider, why) matters.
"""
from __future__ import annotations


def _extract_detail(exc: Exception, s: str) -> str:
    """Pull the provider's own error message out of an OpenAI-SDK-style error.

    Returns a short, prefixed suffix (or "" if nothing useful was found) so the
    caller can append it to the friendly message.
    """
    msg = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
    if not msg:
        msg = s
    msg = msg.strip()
    if not msg:
        return ""
    if len(msg) > 300:
        msg = msg[:300] + "…"
    return f" — provider said: {msg}"


def friendly_api_error(exc: Exception) -> str:
    s = str(exc)
    low = s.lower()
    code = (
        getattr(exc, "status_code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
    )
    detail = _extract_detail(exc, s)

    def _is(target: int, *needles: str) -> bool:
        # Prefer the real status code. Fall back to descriptive text ONLY when no
        # code is available — never key off the bare number in the message.
        if isinstance(code, int):
            return code == target
        return any(n in low for n in needles)

    if _is(429, "rate limit", "rate-limit", "too many requests"):
        return (
            "OpenRouter returned 429 (rate limited). On a paid key this is almost "
            "always the upstream provider for the selected model being at capacity "
            "or a brief request-rate spike — not your credit balance. Retry, switch "
            "to a different model in the sidebar, or cut calls per turn (Tools makes "
            f"2–3; background summary/memory add more).{detail}"
        )
    if _is(402, "insufficient", "credit"):
        return f"OpenRouter reports insufficient credits (402). Add credits or switch model.{detail}"
    if _is(403, "forbidden", "moderat"):
        return (
            "Provider refused the request (403) — usually a temporary rate/policy "
            f"block. Wait and retry, or switch models in the sidebar.{detail}"
        )
    if _is(404, "not found", "no endpoints", "no allowed providers"):
        return (
            "Model unavailable (404). The selected model id may not exist on "
            "OpenRouter or has no provider for this request (e.g. tool use). Pick "
            f"another model in the sidebar.{detail}"
        )
    if _is(423, "locked"):
        return f"Resource temporarily locked (423) — a transient provider issue. Wait and retry.{detail}"
    if _is(401, "unauthor", "api key", "invalid key"):
        return "Authentication failed (401). Check OPENROUTER_API_KEY in the environment."
    if isinstance(code, int) and code >= 500:
        return (
            f"Upstream provider error ({code}) — transient on OpenRouter's side. "
            f"Retry, or switch models if it persists.{detail}"
        )
    return f"Inference failed: {s}"
