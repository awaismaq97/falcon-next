"""
sse.py — Bridge blocking, callback-emitting work into an async event stream.

Falcon's inference engine (falcon.engine, falcon.tools) is built on the
synchronous OpenAI SDK and exposes blocking generators. Rather than rewrite that
battle-tested streaming code for asyncio, we run it in a worker thread and pump
its events onto the running event loop through an asyncio.Queue. The async SSE
endpoint then simply drains the queue.

This keeps the event loop free (no blocking network I/O on it) while reusing the
exact streaming/anti-fabrication/judge logic the Streamlit app shipped.

Usage:
    async for event in run_blocking_stream(lambda emit: work(emit)):
        yield to_sse(event)

`work` receives an ``emit(event: dict)`` callable it may call from the worker
thread as many times as needed; each emitted dict is delivered in order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, AsyncGenerator, Callable

logger = logging.getLogger(__name__)

_SENTINEL = object()


async def run_blocking_stream(
    work: Callable[[Callable[[dict], None]], None],
) -> AsyncGenerator[dict, None]:
    """Run ``work`` in a daemon thread, yielding each event it emits.

    ``work`` is a callable taking a single ``emit(dict)`` argument. It runs in a
    background thread; every event it emits is yielded from this async generator
    in emission order. When ``work`` returns (or raises), the stream ends. A
    raised exception is surfaced as a final ``{"type": "error", ...}`` event so
    the client always receives a terminal signal.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def emit(event: dict) -> None:
        # Called from the worker thread — hand the event to the loop thread-safely.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def runner() -> None:
        try:
            work(emit)
        except Exception as exc:  # noqa: BLE001 - surfaced to client as error event
            logger.exception("streamed work failed: %s", exc)
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "message": str(exc)}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    thread = threading.Thread(target=runner, name="falcon-send-flow", daemon=True)
    thread.start()

    while True:
        event = await queue.get()
        if event is _SENTINEL:
            break
        yield event


def to_sse(event: dict) -> dict:
    """Format a domain event dict as an sse-starlette ServerSentEvent payload.

    We put the event ``type`` in the SSE ``event`` field and JSON-encode the full
    payload in ``data`` so the frontend can switch on the type and read all
    fields from one parse.
    """
    return {"event": event.get("type", "message"), "data": json.dumps(event, default=str)}
