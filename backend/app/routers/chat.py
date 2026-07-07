"""
chat.py — Streaming chat send + payload preview.

POST /chat/send    → text/event-stream (SSE). Structured events drive the UI:
                     meta, token, tool_call, tool_result, message, warning,
                     done, error. See services.chat_service for the event shapes.
POST /chat/preview → the assembled payload + context snapshot WITHOUT calling
                     the model (backs the "Payload Review" toggle).
"""
from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.schemas import ChatPreviewRequest, ChatSendRequest
from app.services.chat_service import build_assembled_payload, run_send_flow
from app.sse import run_blocking_stream, to_sse

router = APIRouter(tags=["chat"])


@router.post("/chat/send")
async def chat_send(req: ChatSendRequest):
    async def event_generator():
        async for event in run_blocking_stream(lambda emit: run_send_flow(req, emit)):
            yield to_sse(event)

    # ping keeps proxies from closing an idle connection while the model thinks.
    # X-Accel-Buffering: no tells nginx-based proxies (incl. DigitalOcean App
    # Platform ingress) not to buffer the SSE stream — without this, tokens
    # arrive in batches instead of as they are generated.
    return EventSourceResponse(
        event_generator(),
        ping=15000,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )


@router.post("/chat/preview")
def chat_preview(req: ChatPreviewRequest) -> dict:
    # Reuse the exact assembly path the send flow uses.
    send_like = ChatSendRequest(
        identity_id=req.identity_id,
        message=req.message,
        images=[],
        documents=req.documents,
        settings=req.settings,
    )
    result = build_assembled_payload(send_like)
    return {
        "raw_payload": result["raw_payload"],
        "annotated_payload": result["annotated_payload"],
        "context_snapshot": result["context_snapshot"],
        "retrieved_entries": result["retrieved_entries"],
        "system_prompt": result["system_prompt"],
        "history_summary": result["history_summary"],
    }
