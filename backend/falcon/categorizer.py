"""
categorizer.py — Background category classification agent for Falcon.

Runs in a background thread after each inference turn (same pattern as
memory_extractor.py). Classifies the completed turn (user message +
assistant response) into exactly ONE of the identity's categories using
openai/gpt-4o-mini via OpenRouter, then persists a full copy to MongoDB.

Hallucination handling
----------------------
The LLM can fail in several ways:
  - Returns a category name that matches nothing in the identity's list
  - Returns empty / missing "category" key in its JSON
  - Returns malformed JSON entirely
  - Returns empty choices (provider error)
  - Times out

In all of these cases the turn is stored in the "Other" fallback category
rather than silently dropped. The stored document carries a
``hallucinated_category`` field with the raw value the LLM returned
(or a descriptive reason string), so every routing decision is auditable.

Public API:
    run(turn_snapshot: dict) -> None
        Entry point called in a background thread.
        turn_snapshot keys:
            identity_id       (str)
            user_message      (str)   — logged user message (may include attachment markers)
            assistant_message (str)   — final response text
            user_ts           (str)   — ISO-8601 user message timestamp
            asst_ts           (str)   — ISO-8601 assistant message timestamp
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# LLM call timeout — keep it short; this runs in a background daemon thread
# and must not stall the server process on a hung OpenRouter connection.
_LLM_TIMEOUT_S = 15

# Categorization prompt — instructs the model to pick exactly one category
# from the identity's available list and return clean JSON.
_CATEGORIZE_PROMPT = """\
You are a message categorization system. Your ONLY job is to assign the \
conversation turn below to exactly ONE of the available categories.

Available categories:
{categories}

Rules:
- Choose the single best-fitting category.
- Base your decision on the TOPIC of the user's message, not the assistant's response.
- Return ONLY valid JSON with the key "category" — no explanation, no extra keys.
- The value MUST exactly match one of the available category names (case-sensitive).
- If the message does not fit any specific category, use "Other".

Format: {{"category": "<category name>"}}

Conversation turn:
User: {user_message}
Assistant: {assistant_message}

Output JSON only:"""

# Regex that strips any markdown code fence (``` or ```json etc.) from the
# model's response before parsing, handling edge cases like missing closing fence.
_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n?(.*?)(?:```.*)?$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences if present."""
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def run(turn_snapshot: dict) -> None:
    """Entry point for background category classification.

    Called in a background thread by chat_service after each inference turn.
    Never raises — all exceptions are caught and logged.
    """
    identity_id = turn_snapshot.get("identity_id", "")
    try:
        _run_categorization(turn_snapshot)
    except Exception as exc:
        logger.error(
            "categorizer: uncaught exception for identity=%r: %s",
            identity_id,
            exc,
            exc_info=True,
        )


def _persist(
    *,
    identity_id: str,
    category_name: str,
    user_message: str,
    assistant_message: str,
    user_ts: str,
    asst_ts: str,
    hallucinated_category: str | None = None,
) -> None:
    """Persist a turn to MongoDB under ``category_name``.

    If ``hallucinated_category`` is set, the document receives an extra field
    recording what the LLM originally returned, for auditability.

    When the target category no longer exists in MongoDB (e.g. user deleted it
    mid-flight), ensure_fallback_category() re-creates "Other" and retries once.
    """
    import falcon.categories as Categories

    # Try the requested category first.
    doc_id = Categories.store_categorized_message(
        identity_id=identity_id,
        category_name=category_name,
        user_message=user_message,
        assistant_response=assistant_message,
        user_ts=user_ts,
        asst_ts=asst_ts,
        hallucinated_category=hallucinated_category,
    )

    if doc_id:
        label = f"'{category_name}'"
        if hallucinated_category:
            label += f" (fallback; LLM said: {hallucinated_category!r})"
        logger.info(
            "categorizer: stored turn under %s for identity=%r (id=%s)",
            label, identity_id, doc_id,
        )
        return

    # Category not found in DB — the user may have deleted "Other".
    # Re-create it and retry once.
    logger.warning(
        "categorizer: category '%s' not found for identity=%r — re-creating and retrying",
        category_name, identity_id,
    )
    Categories.ensure_fallback_category(identity_id)
    doc_id = Categories.store_categorized_message(
        identity_id=identity_id,
        category_name=Categories.FALLBACK_CATEGORY,
        user_message=user_message,
        assistant_response=assistant_message,
        user_ts=user_ts,
        asst_ts=asst_ts,
        hallucinated_category=hallucinated_category or f"(target '{category_name}' deleted)",
    )
    if doc_id:
        logger.info(
            "categorizer: stored turn under fallback 'Other' after retry for identity=%r (id=%s)",
            identity_id, doc_id,
        )
    else:
        logger.error(
            "categorizer: could not persist turn even after re-creating 'Other' for identity=%r",
            identity_id,
        )


def _run_categorization(turn_snapshot: dict) -> None:
    identity_id       = turn_snapshot.get("identity_id", "") or ""
    user_message      = turn_snapshot.get("user_message", "") or ""
    assistant_message = turn_snapshot.get("assistant_message", "") or ""
    user_ts           = turn_snapshot.get("user_ts", "") or ""
    asst_ts           = turn_snapshot.get("asst_ts", "") or ""

    if not identity_id:
        logger.error("categorizer: missing identity_id in turn_snapshot")
        return

    # Skip trivially empty turns (e.g. pure attachment-only with no text).
    if not user_message.strip() and not assistant_message.strip():
        logger.debug("categorizer: skipping empty turn for identity=%r", identity_id)
        return

    # Lazy imports to avoid circular imports at module load time.
    try:
        import falcon.config as Config
        import falcon.categories as Categories
        from falcon.engine import get_client
    except ImportError as exc:
        logger.error("categorizer: import error: %s", exc)
        return

    # Fetch current categories for the identity (also seeds defaults if missing).
    try:
        cats = Categories.list_categories(identity_id)
        category_names = [c["name"] for c in cats]
    except Exception as exc:
        logger.error(
            "categorizer: failed to load categories for identity=%r: %s", identity_id, exc
        )
        return

    if not category_names:
        logger.warning(
            "categorizer: no categories found for identity=%r — skipping", identity_id
        )
        return

    # ── LLM call ──────────────────────────────────────────────────────────

    category_list = "\n".join(f"- {name}" for name in category_names)
    prompt = _CATEGORIZE_PROMPT.format(
        categories=category_list,
        user_message=user_message[:2000],
        assistant_message=assistant_message[:2000],
    )

    raw = ""
    llm_failed = False

    try:
        client = get_client(Config.OPENROUTER_API_KEY, title="Falcon-Categorizer")
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=64,
            timeout=_LLM_TIMEOUT_S,
        )

        if not response.choices:
            logger.error(
                "categorizer: LLM returned empty choices for identity=%r — falling back to Other",
                identity_id,
            )
            llm_failed = True
        else:
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                logger.error(
                    "categorizer: LLM returned empty content for identity=%r — falling back to Other",
                    identity_id,
                )
                llm_failed = True

    except Exception as exc:
        logger.error(
            "categorizer: LLM call failed for identity=%r: %s — falling back to Other",
            identity_id, exc,
        )
        llm_failed = True

    if llm_failed:
        _persist(
            identity_id=identity_id,
            category_name=Categories.FALLBACK_CATEGORY,
            user_message=user_message,
            assistant_message=assistant_message,
            user_ts=user_ts,
            asst_ts=asst_ts,
            hallucinated_category="(LLM call failed or returned empty response)",
        )
        return

    # ── Parse JSON ─────────────────────────────────────────────────────────

    chosen_category: str | None = None
    parse_failure_reason: str | None = None

    try:
        cleaned = _strip_code_fence(raw)
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
        chosen_category = str(parsed.get("category", "")).strip() or None
    except (json.JSONDecodeError, ValueError) as exc:
        parse_failure_reason = f"malformed JSON: {exc} (raw: {raw[:100]!r})"
        logger.warning(
            "categorizer: %s for identity=%r — falling back to Other",
            parse_failure_reason, identity_id,
        )

    if parse_failure_reason or not chosen_category:
        _persist(
            identity_id=identity_id,
            category_name=Categories.FALLBACK_CATEGORY,
            user_message=user_message,
            assistant_message=assistant_message,
            user_ts=user_ts,
            asst_ts=asst_ts,
            hallucinated_category=parse_failure_reason or "(empty category in JSON response)",
        )
        return

    # ── Validate category against known list ──────────────────────────────
    # 1. Exact match
    # 2. Case-insensitive match (handles "technology" → "Technology")
    # 3. Fallback to Other, recording the hallucinated value

    if chosen_category not in category_names:
        lower_map = {name.lower(): name for name in category_names}
        canonical = lower_map.get(chosen_category.lower())
        if canonical:
            logger.info(
                "categorizer: case-insensitive match '%s' → '%s' for identity=%r",
                chosen_category, canonical, identity_id,
            )
            chosen_category = canonical
        else:
            # True hallucination — unknown category name.
            logger.warning(
                "categorizer: LLM hallucinated category %r for identity=%r — storing in Other",
                chosen_category, identity_id,
            )
            _persist(
                identity_id=identity_id,
                category_name=Categories.FALLBACK_CATEGORY,
                user_message=user_message,
                assistant_message=assistant_message,
                user_ts=user_ts,
                asst_ts=asst_ts,
                hallucinated_category=chosen_category,
            )
            return

    # ── Normal path: known category ────────────────────────────────────────
    _persist(
        identity_id=identity_id,
        category_name=chosen_category,
        user_message=user_message,
        assistant_message=assistant_message,
        user_ts=user_ts,
        asst_ts=asst_ts,
    )
