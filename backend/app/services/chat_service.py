"""
chat_service.py — Faithful port of the Streamlit _handle_send inference flow.

This runs in a worker thread (see app.sse.run_blocking_stream) and drives the
entire turn: log user message → retrieve memory → assemble annotated payload →
stream (or buffer, for judge) the model → optional tools agent → optional judge
→ log assistant message → persist trace → fire background tasks
(audit, tokens, memory extraction, summary, dual-run).

Every step calls ``emit(event)`` so the SSE endpoint can forward structured
events to the browser:

    {"type": "meta",        "user_ts", "model", "config", ...}
    {"type": "token",       "text"}                      # streamed answer chunk
    {"type": "tool_call",   "tool", "args"}
    {"type": "tool_result", "tool", "content"}
    {"type": "message",     "text"}                      # judge-mode final reveal
    {"type": "warning",     "message"}
    {"type": "done",        "response_text", "usage", "latency_ms",
                            "suppressed", "judge", "asst_ts", "tokens_total"}
    {"type": "error",       "message"}

Because generation reuses the exact falcon.engine / falcon.tools streaming code,
the anti-fabrication guard, <think>-stripping, and judge behaviour are identical
to the Streamlit app.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import falcon.audit as Audit
import falcon.config as Config
import falcon.dual_run as DualRun
import falcon.engine as Engine
import falcon.identity as Identity
import falcon.judge as Judge
import falcon.logger as Logger
import falcon.memory as Memory
import falcon.summarizer as Summarizer
import falcon.tools as Tools
from app.errors import friendly_api_error
from app.schemas import ChatSendRequest
from falcon.db import get_db

logger = logging.getLogger("falcon.chat")

Emit = Callable[[dict], None]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_image_markdown(text: str, *urls: str) -> bool:
    candidates = "|".join(re.escape(u) for u in urls if u)
    if not candidates:
        return False
    return re.search(r"!\[[^\]]*\]\((?:" + candidates + r")\)", text) is not None


def _compose_with_documents(message: str, documents) -> str:
    """Append extracted document text to the user message for the model payload."""
    if not documents:
        return message
    blocks = []
    for d in documents:
        name = getattr(d, "filename", "document")
        text = getattr(d, "text", "") or ""
        if text.strip():
            blocks.append(f"--- Attached document: {name} ---\n{text}\n--- End of {name} ---")
    if not blocks:
        return message
    doc_text = "\n\n".join(blocks)
    return f"{message}\n\n{doc_text}" if message.strip() else doc_text


def _attachment_marker(images, documents) -> str:
    """Compact marker persisted in place of raw image/document content."""
    parts = []
    if images:
        n = len(images)
        parts.append(f"🖼 _{n} image{'s' if n != 1 else ''} attached_")
    if documents:
        names = ", ".join(getattr(d, "filename", "document") for d in documents)
        parts.append(f"📎 _{names}_")
    return "\n\n".join(parts)


def build_assembled_payload(req: ChatSendRequest) -> dict:
    """Assemble the annotated payload + context snapshot WITHOUT calling the model.

    Shared by the /chat/preview endpoint (Payload Review) and the send flow.
    Returns a dict with raw_payload, annotated_payload, context_snapshot,
    retrieved_entries, system_prompt, history, and resolved settings.
    """
    s = req.settings
    identity_id = req.identity_id
    system_prompt = s.system_prompt_text.strip() if s.use_system_prompt else ""
    history_summary = None
    if s.history_mode in ("summary", "hybrid"):
        history_summary = Summarizer.get_summary(identity_id)

    try:
        retrieval = Memory.retrieve_for_generation(
            identity_id=identity_id,
            query=req.message,
            top_k_per_type=Config.top_k_per_type,
            recency_weight=Config.recency_weight,
            relevance_weight=Config.relevance_weight,
        )
        retrieved_entries = retrieval.entries
    except Exception:  # noqa: BLE001
        retrieval = None
        retrieved_entries = []

    if not s.use_persona:
        retrieved_entries = [e for e in retrieved_entries if e.get("memory_type") != "persona"]
        # Keep retrieval display in sync with the filtered entries so the
        # context viewer shows what was actually used, not the raw DB fetch.
        if retrieval is not None:
            retrieval.entries = retrieved_entries
            retrieval.by_type = {k: v for k, v in retrieval.by_type.items() if k != "persona"}

    history = Identity.load_history(identity_id)
    messages_for_model = [{"role": m["role"], "content": m["content"]} for m in history]
    # Attached documents are injected into this turn's user message (after memory
    # retrieval, which queries on the typed text only) so the model can read them.
    current_content = _compose_with_documents(req.message, getattr(req, "documents", None))
    messages_for_model.append({"role": "user", "content": current_content})

    try:
        annotated_payload, context_snapshot = Engine.build_annotated_payload(
            system_prompt=system_prompt,
            messages=messages_for_model,
            memory_block=retrieved_entries,
            truncation_strategy="last-n-turns",
            history_max_turns=s.history_max_turns,
            history_mode=s.history_mode,
            history_summary=history_summary,
        )
        if retrieval is not None:
            context_snapshot["retrieval_result"] = retrieval.to_display_dict()
        raw_payload = [{"role": e["role"], "content": e["content"]} for e in annotated_payload]
    except Exception:  # noqa: BLE001
        annotated_payload = []
        raw_payload = Engine.build_payload(system_prompt, messages_for_model)
        context_snapshot = Engine.build_context_view(
            system_prompt=system_prompt,
            messages=messages_for_model,
            retrieved_memory=retrieved_entries,
        )

    return {
        "system_prompt": system_prompt,
        "history": history,
        "retrieved_entries": retrieved_entries,
        "annotated_payload": annotated_payload,
        "raw_payload": raw_payload,
        "context_snapshot": context_snapshot,
        "history_summary": history_summary,
    }


def run_send_flow(req: ChatSendRequest, emit: Emit) -> None:
    """Execute one full inference turn, emitting SSE events along the way."""
    s = req.settings
    identity_id = req.identity_id
    user_input = req.message
    images = req.images or []
    documents = req.documents or []
    gen = {
        "temperature": s.generation.temperature,
        "top_p": s.generation.top_p,
        "repetition_penalty": s.generation.repetition_penalty,
        "stop_tokens": s.generation.stop_tokens,
    }
    model = s.model
    use_judge = s.use_judge
    judge_model = s.judge_model or (
        Config.available_models[0] if Config.available_models else Config.default_model
    )
    use_tools = s.use_tools

    # Image turns → vision model, tools disabled (multimodal + ReAct out of scope).
    if images:
        model = Config.vision_model
        use_tools = False

    system_prompt = s.system_prompt_text.strip() if s.use_system_prompt else ""
    prompt_state = "present" if system_prompt else "empty"

    trace: list[dict] = []
    t0 = time.monotonic()

    def push(stage: str, data, status: str = "info"):
        trace.append(
            {
                "t": _ts(),
                "stage": stage,
                "data": data,
                "status": status,
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
            }
        )

    push(
        "config",
        {
            "model": model,
            "temperature": gen["temperature"],
            "top_p": gen["top_p"],
            "repetition_penalty": gen["repetition_penalty"],
            "stop_tokens": gen["stop_tokens"],
            "identity": identity_id,
            "prompt_state": prompt_state,
            "truncation_strategy": "last-n-turns",
            "history_mode": s.history_mode,
            "judge_enabled": use_judge,
            "judge_model": judge_model if use_judge else None,
            "tools_enabled": use_tools,
            "tools": Tools.tool_names() if use_tools else [],
        },
    )

    user_ts = _utc_iso()

    # Persisted user text carries a marker when images/documents were attached
    # (raw content is never stored) so past turns visibly show what was sent.
    logged_user_input = user_input
    marker = _attachment_marker(images, documents)
    if marker:
        logged_user_input = f"{user_input}\n\n{marker}" if user_input.strip() else marker

    emit(
        {
            "type": "meta",
            "user_ts": user_ts,
            "model": model,
            "logged_user_input": logged_user_input,
            "tools_enabled": use_tools,
            "judge_enabled": use_judge,
        }
    )

    # ── Assemble payload BEFORE logging the user message ──────────────────
    # build_assembled_payload reads history from the DB and appends the current
    # message itself. If we logged first, load_history would already contain this
    # turn and the user message would appear twice in the payload (and be sent to
    # the model twice). So assemble against the pre-turn history, then log.
    assembled = build_assembled_payload(req)
    system_prompt = assembled["system_prompt"]
    prompt_state = "present" if system_prompt else "empty"
    retrieved_entries = assembled["retrieved_entries"]
    raw_payload = assembled["raw_payload"]
    context_snapshot = assembled["context_snapshot"]
    history = assembled["history"]

    # Log user message (after assembly so history didn't double-count it).
    try:
        Logger.append_message(identity_id, "user", logged_user_input, timestamp=user_ts)
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": f"Failed to log user message: {exc}"})
        return

    push("memory retrieved", context_snapshot.get("retrieval_result", {}))
    push("payload built", {"message_count": len(raw_payload), "payload": raw_payload})

    # ── Multimodal variant used ONLY for the model call (no base64 in storage)
    model_payload = raw_payload
    if images:
        model_payload = [dict(m) for m in raw_payload]
        last = dict(model_payload[-1])
        text = last.get("content", "")
        last["content"] = (
            ([{"type": "text", "text": text}] if text else [])
            + [{"type": "image_url", "image_url": {"url": u}} for u in images]
        )
        model_payload[-1] = last

    # ── Generate ──────────────────────────────────────────────────────────
    api_t0 = time.monotonic()
    response_text = ""
    usage: dict = {}
    raw_output = ""
    suppressed = False
    judge_payload = None

    stream_gen = Engine.stream_inference(
        model_name=model,
        payload=model_payload,
        api_key=Config.OPENROUTER_API_KEY,
        temperature=gen["temperature"],
        top_p=gen["top_p"],
        repetition_penalty=gen["repetition_penalty"],
        stop_tokens=gen["stop_tokens"],
    )

    try:
        if use_tools:
            push(
                "→ LangGraph agent call (tools enabled)",
                {"model": model, "tools": Tools.tool_names(), "messages_count": len(raw_payload)},
            )
            agent_stream = Tools.stream_agent(
                payload=raw_payload,
                model_name=model,
                api_key=Config.OPENROUTER_API_KEY,
                temperature=gen["temperature"],
                top_p=gen["top_p"],
            )
            if use_judge:
                response_text = "".join(list(agent_stream))  # buffer for judge
            else:
                for tok in agent_stream:
                    response_text += tok
                    emit({"type": "token", "text": tok})

            for ev in agent_stream.events:
                if ev["type"] == "tool_call":
                    push(f"→ tool call: {ev['tool']}", {"args": ev["args"]})
                    emit({"type": "tool_call", "tool": ev["tool"], "args": ev["args"]})
                else:
                    push(f"← tool result: {ev['tool']}", {"content": ev["content"]}, status="success")
                    emit({"type": "tool_result", "tool": ev["tool"], "content": ev["content"]})

            if not response_text or not response_text.strip():
                response_text = "[no output]"

            # Embed APOD images as markdown so they persist + render on reload.
            image_md = ""
            for img in agent_stream.images:
                if not _has_image_markdown(response_text, img["url"], img.get("hdurl") or ""):
                    image_md += f"\n\n![{img['title']}]({img['url']})"
                    if img.get("hdurl") and img["hdurl"] != img["url"]:
                        image_md += f"\n\n[🔭 View full resolution]({img['hdurl']})"
            if image_md:
                response_text += image_md
                if not use_judge:
                    emit({"type": "token", "text": image_md})

            usage = agent_stream.usage
            raw_output = response_text

        elif use_judge:
            push("→ generator call (buffered — judge mode ON)", {"model": model})
            tokens = list(stream_gen)  # exhaust silently
            response_text = "".join(tokens)
            if not response_text or not response_text.strip():
                response_text = "[no output]"
            usage = stream_gen.usage
            raw_output = getattr(stream_gen, "raw_output", response_text)

        else:
            push("→ OpenRouter API call (streaming)", {"model": model, "messages_count": len(raw_payload)})
            for tok in stream_gen:
                response_text += tok
                emit({"type": "token", "text": tok})
            if not response_text or not response_text.strip():
                response_text = "[no output]"
                emit({"type": "token", "text": response_text})
            usage = stream_gen.usage
            raw_output = getattr(stream_gen, "raw_output", response_text)
    except Exception as exc:  # noqa: BLE001
        msg = friendly_api_error(exc)
        push("ERROR — generation", str(exc), status="error")
        # Falcon "always output": complete the turn with a visible marker so the
        # pairing stays intact and the next send works normally.
        try:
            Logger.append_message(identity_id, "assistant", f"⚠️ {msg}", timestamp=_utc_iso())
        except Exception:  # noqa: BLE001
            pass
        emit({"type": "error", "message": msg})
        return

    api_latency_ms = round((time.monotonic() - api_t0) * 1000)
    push("← generation complete", {"latency_ms": api_latency_ms, "content_preview": response_text[:200]})

    # ── Judge ─────────────────────────────────────────────────────────────
    if use_judge:
        push("→ judge call", {"judge_model": judge_model})
        try:
            jr = Judge.evaluate(
                response_text=response_text,
                user_input=user_input,
                model=judge_model,
                api_key=Config.OPENROUTER_API_KEY,
                system_prompt=Config.judge_system_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            push("ERROR — judge call", str(exc), status="error")
            jr = None
        if jr is not None:
            judge_payload = {
                "verdict": jr.verdict,
                "reason": jr.reason,
                "latency_ms": jr.latency_ms,
                "model": jr.model,
                "raw": jr.raw,
                "error": jr.error or None,
            }
            push(
                f"← judge verdict: {jr.verdict}",
                judge_payload,
                status="success" if jr.verdict == "pass" else "warn",
            )
            if jr.verdict == "suppress":
                suppressed = True
                response_text = "[suppressed]"
        # Judge mode buffered the answer — reveal the final text now.
        emit({"type": "message", "text": response_text})

    # ── Assistant-language warning (system prompt OFF) ────────────────────
    if not suppressed and not s.use_system_prompt:
        for pattern in Config.assistant_language_patterns:
            if pattern.lower() in response_text.lower():
                emit(
                    {
                        "type": "warning",
                        "message": f"Assistant-language pattern detected (system prompt OFF): `{pattern}`",
                    }
                )
                break

    # ── Persist assistant message + trace ─────────────────────────────────
    asst_ts = _utc_iso()
    try:
        Logger.append_message(identity_id, "assistant", response_text, timestamp=asst_ts)
    except Exception as exc:  # noqa: BLE001
        push("ERROR — log assistant response", str(exc), status="error")

    snapshot = {
        "user_timestamp": user_ts,
        "send_timestamp": _ts(),
        "user": user_input,
        "steps": trace,
        "context_snapshot": context_snapshot,
    }
    try:
        get_db()["traces"].insert_one({"identity_id": identity_id, **snapshot})
    except Exception as exc:  # noqa: BLE001
        logger.error("trace insert failed for identity=%s: %s", identity_id, exc)

    # ── Token totals (cumulative per identity) ────────────────────────────
    tokens_total = _increment_tokens(identity_id, usage)

    emit(
        {
            "type": "done",
            "response_text": response_text,
            "raw_output": raw_output,
            "usage": usage,
            "latency_ms": api_latency_ms,
            "suppressed": suppressed,
            "judge": judge_payload,
            "user_ts": user_ts,
            "asst_ts": asst_ts,
            "tokens_total": tokens_total,
            "logged_user_input": logged_user_input,
        }
    )

    # ── Background tasks (client already has its answer) ──────────────────
    final_history = list(history) + [
        {"timestamp": user_ts, "role": "user", "content": logged_user_input},
        {"timestamp": asst_ts, "role": "assistant", "content": response_text},
    ]
    _launch_background_tasks(
        req=req,
        model=model,
        prompt_state=prompt_state,
        system_prompt=system_prompt,
        gen=gen,
        retrieved_entries=retrieved_entries,
        raw_payload=raw_payload,
        context_snapshot=context_snapshot,
        raw_output=raw_output,
        usage=usage,
        latency_ms=api_latency_ms,
        response_text=response_text,
        user_input=user_input,
        final_history=final_history,
    )


# ---------------------------------------------------------------------------
# Token accounting + background tasks
# ---------------------------------------------------------------------------

def _increment_tokens(identity_id: str, usage: dict) -> dict:
    if not usage:
        doc = get_db()["tokens"].find_one({"identity_id": identity_id}, {"_id": 0})
        return {
            "prompt": (doc or {}).get("prompt", 0),
            "completion": (doc or {}).get("completion", 0),
            "total": (doc or {}).get("total", 0),
        }
    db = get_db()
    doc = db["tokens"].find_one_and_update(
        {"identity_id": identity_id},
        {
            "$inc": {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            }
        },
        upsert=True,
        return_document=True,
    )
    return {
        "prompt": (doc or {}).get("prompt", 0),
        "completion": (doc or {}).get("completion", 0),
        "total": (doc or {}).get("total", 0),
    }


def _launch_background_tasks(
    *,
    req: ChatSendRequest,
    model: str,
    prompt_state: str,
    system_prompt: str,
    gen: dict,
    retrieved_entries: list,
    raw_payload: list,
    context_snapshot: dict,
    raw_output: str,
    usage: dict,
    latency_ms: float,
    response_text: str,
    user_input: str,
    final_history: list,
) -> None:
    identity_id = req.identity_id
    s = req.settings

    def _bg_audit():
        try:
            if Config.audit_enabled:
                record = Audit.build_audit_record(
                    identity_id=identity_id,
                    model=model,
                    prompt_state=prompt_state,
                    system_prompt=system_prompt if system_prompt else None,
                    retrieved_memories=[
                        {"type": e.get("memory_type"), "content": e.get("content")}
                        for e in retrieved_entries
                    ],
                    generation_settings=gen,
                    context_size=len(raw_payload),
                    context_token_estimate=context_snapshot.get("context_token_estimate", 0),
                    assembled_payload=raw_payload,
                    raw_model_output=raw_output,
                    usage=usage or {},
                    latency_ms=latency_ms,
                )
                Audit.write_audit_record(identity_id, record)
        except Exception as exc:  # noqa: BLE001
            logger.error("bg_audit failed for identity=%s: %s", identity_id, exc)

    def _bg_extractor():
        try:
            if Config.memory_extraction_enabled:
                import falcon.memory_extractor as MemoryExtractor

                MemoryExtractor.run(
                    {
                        "identity_id": identity_id,
                        "user_message": user_input,
                        "assistant_message": response_text,
                        "turn_index": len(final_history) // 2,
                        "timestamp": _utc_iso(),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("bg_extractor failed for identity=%s: %s", identity_id, exc)

    def _bg_summarizer():
        try:
            Summarizer.update_summary(
                identity_id=identity_id,
                history=final_history,
                model=Config.summary_model,
                api_key=Config.OPENROUTER_API_KEY,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("bg_summarizer failed for identity=%s: %s", identity_id, exc)

    def _bg_dual_run():
        try:
            persona_content = ""
            try:
                pe = Memory.get_memories(identity_id, memory_type="persona", limit=1)
                if pe:
                    persona_content = pe[0].get("content", "")
            except Exception:  # noqa: BLE001
                pass
            record = DualRun.run_dual(
                payload=raw_payload,
                model=model,
                api_key=Config.OPENROUTER_API_KEY,
                gen_settings=gen,
                identity_id=identity_id,
                system_prompt=system_prompt,
                state_tag=s.dual_run_state_tag,
                user_input=user_input,
                persona_content=persona_content,
            )
            DualRun.write_record(record)
        except Exception as exc:  # noqa: BLE001
            logger.error("bg_dual_run failed for identity=%s: %s", identity_id, exc)

    threading.Thread(target=_bg_audit, daemon=True).start()
    threading.Thread(target=_bg_extractor, daemon=True).start()
    # Summary only when the active history mode will consume it (raw never does).
    if s.history_mode in ("summary", "hybrid"):
        threading.Thread(target=_bg_summarizer, daemon=True).start()
    if s.dual_run_enabled:
        threading.Thread(target=_bg_dual_run, daemon=True).start()
