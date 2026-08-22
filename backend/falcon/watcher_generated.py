"""
watcher_generated.py — Persistent store + loader for dynamically spawned watcher tools.

Tools created at runtime by ``spawn_agent`` used to be appended straight into
``watcher_tools.py``. That had two fatal problems:

  1. Under ``uvicorn --reload`` the write to a ``.py`` file inside the reload
     watch set restarted the whole server, killing every watcher thread
     mid-command and discarding the in-memory registry it had just updated.
  2. Without ``--reload`` a long-lived process kept whatever registry it booted
     with, so a tool written by one process was invisible to another — the
     "[ERROR] Unknown command: 'spawn_agent'" class of failure.

Generated tool source now lives in the ``watcher_generated_tools`` collection
instead. Nothing on disk changes, so no reload fires, and Mongo is the single
source of truth shared by every process. A registry that has never seen a tool
can heal itself on demand via :func:`load_one`.

Document shape::

    {
      "name":       "shopping_tool",       # unique, snake_case
      "code":       "def _shopping_tool(payload: str) -> str: ...",
      "context":    "<original natural-language request>",
      "created_at": "2026-08-22T11:15:28Z",
      "revision":   1,
    }

The stored ``code`` is the bare function body — the ``@register_tool(...)``
decorator line is synthesised at load time so the registered name can never
drift from the document key.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from falcon.db import get_db

logger = logging.getLogger("falcon.watcher_generated")

COLLECTION = "watcher_generated_tools"

# Set once the collection has been swept into the live registry for this
# process. Individual misses still heal through load_one().
_bulk_loaded = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coll():
    db = get_db()
    coll = db[COLLECTION]
    try:
        coll.create_index("name", unique=True)
    except Exception as exc:  # noqa: BLE001 — index is an optimisation, not correctness
        logger.debug("watcher_generated: index ensure failed: %s", exc)
    return coll


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save(name: str, code: str, context: str = "") -> None:
    """Upsert one generated tool. Bumps ``revision`` when the code changes."""
    _coll().update_one(
        {"name": name},
        {
            "$set": {"name": name, "code": code, "context": context},
            "$setOnInsert": {"created_at": _utc_now_iso()},
            "$inc": {"revision": 1},
        },
        upsert=True,
    )
    logger.info("watcher_generated: saved tool %r (%d chars)", name, len(code))


def get(name: str) -> dict | None:
    """Return the stored document for one tool, or None."""
    try:
        return _coll().find_one({"name": name}, {"_id": 0})
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_generated: get(%r) failed: %s", name, exc)
        return None


def list_all() -> list[dict]:
    """Return every stored generated tool, oldest first."""
    try:
        return list(_coll().find({}, {"_id": 0}).sort("created_at", 1))
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_generated: list_all failed: %s", exc)
        return []


def delete(name: str) -> bool:
    """Remove a generated tool from the store and the live registry."""
    try:
        removed = _coll().delete_one({"name": name}).deleted_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_generated: delete(%r) failed: %s", name, exc)
        return False
    if removed:
        registry = _registry()
        if registry is not None:
            registry.pop(name, None)
        logger.info("watcher_generated: deleted tool %r", name)
    return removed


# ---------------------------------------------------------------------------
# Loading into the live registry
# ---------------------------------------------------------------------------

def _tools_module():
    """Return the canonical falcon.watcher_tools module, importing if needed."""
    mod = sys.modules.get("falcon.watcher_tools")
    if mod is None:
        import falcon.watcher_tools  # noqa: F401 — force canonical load
        mod = sys.modules["falcon.watcher_tools"]
    return mod


def _registry() -> dict | None:
    return getattr(_tools_module(), "_REGISTRY", None)


def compile_check(name: str, code: str) -> str:
    """Compile the decorated form of ``code`` without running it.

    Returns an empty string when the source is valid, otherwise a human-readable
    reason. Used by spawn_agent to reject broken generations *before* they are
    persisted, so a bad LLM output can never poison the store.
    """
    try:
        compile(_decorated(name, code), f"<watcher_generated:{name}>", "exec")
    except SyntaxError as exc:
        return f"syntax error on line {exc.lineno}: {exc.msg}"
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return ""


def _decorated(name: str, code: str) -> str:
    """Prepend the register_tool decorator so the name always matches the key."""
    return f"@register_tool({name!r})\n{code.strip()}\n"


def exec_into_registry(name: str, code: str) -> bool:
    """Execute one generated tool so it registers into the live registry.

    The code runs in its own namespace rather than the watcher_tools module
    globals, so a generated function can never shadow a built-in tool or module
    attribute. It still lands in the shared registry because ``register_tool``
    closes over it.
    """
    mod = _tools_module()
    registry = getattr(mod, "_REGISTRY", None)
    if registry is None:
        logger.error("watcher_generated: watcher_tools has no _REGISTRY, cannot load %r", name)
        return False

    namespace: dict = {
        "__name__": f"falcon.watcher_generated.{name}",
        "__builtins__": __builtins__,
        "register_tool": mod.register_tool,
    }
    try:
        exec(compile(_decorated(name, code), f"<watcher_generated:{name}>", "exec"), namespace)  # noqa: S102
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_generated: exec of %r failed: %s", name, exc)
        return False

    if name not in registry:
        # The decorator is synthesised, so this only happens if the generated
        # source contains no function for it to decorate.
        logger.error("watcher_generated: %r executed but registered nothing", name)
        return False
    return True


def load_one(name: str) -> bool:
    """Load a single generated tool from Mongo into the live registry.

    This is the self-healing path: ``dispatch`` calls it when it sees a command
    it does not recognise, so a tool spawned by another process (or before this
    process booted) resolves on first use instead of erroring out.
    """
    doc = get(name)
    if not doc:
        return False
    return exec_into_registry(doc["name"], doc.get("code", ""))


def load_all(force: bool = False) -> int:
    """Load every stored generated tool into the live registry.

    Called once at startup. Returns the number successfully registered. Safe to
    call repeatedly — re-executing a tool just rebinds it in the registry.
    """
    global _bulk_loaded
    if _bulk_loaded and not force:
        return 0

    docs = list_all()
    loaded = 0
    for doc in docs:
        if exec_into_registry(doc["name"], doc.get("code", "")):
            loaded += 1

    _bulk_loaded = True
    if docs:
        logger.info("watcher_generated: loaded %d/%d generated tool(s)", loaded, len(docs))
    return loaded


def ensure_loaded() -> None:
    """Idempotent startup hook — loads generated tools once per process."""
    if _bulk_loaded:
        return
    try:
        load_all()
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logger.error("watcher_generated: ensure_loaded failed: %s", exc)
