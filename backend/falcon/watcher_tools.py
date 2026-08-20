"""
watcher_tools.py — Tool registry for the Conversation Watcher-Agent.

Each tool is a callable that receives a payload string and returns a result
string. Tools that require external API credentials check for them at call time
and return a clear [NOT CONFIGURED] message when absent, so the watcher never
fails silently.

Registering a real tool later:
    @register_tool("post_tweet")
    def _post_tweet(payload: str) -> str:
        ...

Built-in tools (v1 — dummy/safe):
  echo         — returns the payload unchanged (debug/test)
  ping         — returns a UTC timestamp (heartbeat)
  http_get     — fetches a URL and returns the first 2 000 chars of the body
  post_tweet   — stub (NOT CONFIGURED until Twitter credentials are wired)
  fetch_replies— stub (NOT CONFIGURED)
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Registry: command_name → handler(payload: str) -> str
_REGISTRY: dict[str, Callable[[str], str]] = {}


def register_tool(name: str) -> Callable:
    """Decorator that registers a function as a named watcher tool."""
    def _dec(fn: Callable[[str], str]) -> Callable[[str], str]:
        _REGISTRY[name.lower()] = fn
        return fn
    return _dec


def dispatch(command: str, payload: str) -> str:
    """Dispatch command → tool handler. Returns an error string for unknown commands."""
    handler = _REGISTRY.get(command.lower().strip())
    if handler is None:
        known = ", ".join(sorted(_REGISTRY.keys()))
        return f"[ERROR] Unknown command: {command!r}. Available: {known}"
    try:
        return handler(payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_tools: tool %r raised: %s", command, exc)
        return f"[ERROR] Tool '{command}' raised an exception: {exc}"


def list_tools() -> list[str]:
    """Return sorted list of registered tool names."""
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------

@register_tool("echo")
def _echo(payload: str) -> str:
    """Return the payload as-is. Useful for testing the watcher pipeline."""
    return payload.strip() or "(empty payload)"


@register_tool("ping")
def _ping(payload: str) -> str:
    """Return the current UTC timestamp. Confirms the watcher is alive."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"pong — watcher alive at {ts}"


@register_tool("http_get")
def _http_get(payload: str) -> str:
    """Fetch a URL (GET only) and return the first 2 000 characters of the body."""
    import requests

    url = payload.strip()
    if not url:
        return "[ERROR] http_get requires a URL as payload."
    # Basic safety: only allow http/https schemes.
    if not url.lower().startswith(("http://", "https://")):
        return "[ERROR] http_get only accepts http:// or https:// URLs."
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
        body = resp.text[:2000]
        return f"HTTP {resp.status_code} — {url}\n\n{body}"
    except requests.RequestException as exc:
        return f"[ERROR] http_get failed: {exc}"


@register_tool("post_tweet")
def _post_tweet(payload: str) -> str:
    """Post a tweet. [NOT CONFIGURED — wire TWITTER_API_KEY etc. to enable]"""
    return (
        "[NOT CONFIGURED] post_tweet requires TWITTER_API_KEY, TWITTER_API_SECRET, "
        "TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET environment variables."
    )


@register_tool("fetch_replies")
def _fetch_replies(payload: str) -> str:
    """Fetch replies to a tweet URL or ID. [NOT CONFIGURED]"""
    return (
        "[NOT CONFIGURED] fetch_replies requires Twitter API credentials. "
        "Set TWITTER_API_KEY etc. to enable."
    )
