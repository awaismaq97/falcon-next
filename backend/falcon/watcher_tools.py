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
  research     — long-running search → browse → summarize. Starts a background
                 job whose state lives in MongoDB, so it survives restarts and
                 can be collected days later. See falcon/research.py.

Dynamic tools (v2):
  spawn_agent  — uses GPT-4o-mini to generate a new stub tool, stores its source
                 in the ``watcher_generated_tools`` Mongo collection, loads it
                 into the live registry, and then triggers a watcher-persona
                 refresh so every identity knows about the new capability.

Generated tools are deliberately NOT written back into this file. See
falcon/watcher_generated.py for why (uvicorn reload storms + per-process
registry drift).
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import textwrap
from typing import Callable

logger = logging.getLogger(__name__)

# Registry: command_name → handler(payload: str) -> str
_REGISTRY: dict[str, Callable[[str], str]] = {}

# Which identity's conversation triggered the tool currently executing.
#
# Handlers take only a payload, so a tool that owns per-user state (research
# jobs) would otherwise have no way to scope it — and in a multi-user app,
# unscoped state means one user's work is visible to everyone. The watcher sets
# this immediately before dispatch; a contextvar rather than a global because
# each watcher thread must see its own value.
_current_identity: contextvars.ContextVar[str] = contextvars.ContextVar(
    "falcon_watcher_identity", default=""
)


def set_current_identity(identity_id: str) -> None:
    """Record whose conversation is being served, for the duration of a dispatch."""
    _current_identity.set(identity_id or "")


def current_identity() -> str:
    """The identity the running tool is acting for, or "" when unknown."""
    return _current_identity.get()

# Tools that may never be removed through the management API, on top of the
# blanket rule that built-ins are not deletable. spawn_agent is the only way to
# create a new tool, so deleting it would leave the system unable to grow one
# back without a code change and redeploy.
PROTECTED_TOOLS: frozenset[str] = frozenset({"spawn_agent"})


def register_tool(name: str) -> Callable:
    """Decorator that registers a function as a named watcher tool."""
    def _dec(fn: Callable[[str], str]) -> Callable[[str], str]:
        _REGISTRY[name.lower()] = fn
        return fn
    return _dec


def dispatch(command: str, payload: str) -> str:
    """Dispatch command → tool handler. Returns an error string for unknown commands.

    A command missing from the in-memory registry is not immediately an error:
    it may be a tool spawned by another process, or one spawned before this
    process booted. We consult the generated-tool store once before giving up,
    so the registry heals itself on first use.
    """
    name = command.lower().strip()
    handler = _REGISTRY.get(name)

    if handler is None:
        import falcon.watcher_generated as Generated
        if Generated.load_one(name):
            handler = _REGISTRY.get(name)
            logger.info("dispatch: healed unknown command %r from the generated-tool store", name)

    if handler is None:
        known = ", ".join(sorted(_REGISTRY.keys()))
        return f"[ERROR] Unknown command: {command!r}. Available: {known}"

    try:
        return handler(payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("watcher_tools: tool %r raised: %s", command, exc)
        return f"[ERROR] Tool '{command}' raised an exception: {exc}"


def list_tools() -> list[str]:
    """Return sorted list of registered tool names, including generated ones."""
    import falcon.watcher_generated as Generated
    Generated.ensure_loaded()
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


# ---------------------------------------------------------------------------
# research — long-running search / browse / summarize
# ---------------------------------------------------------------------------

# Sub-commands are recognised only on an exact match, so an actual research
# question is never mistaken for one. "status of the UK grid in 2026" starts a
# job; "status a1b2c3" inspects one.
_RESEARCH_CMD = re.compile(r"^(status|result|cancel)(?:\s+([0-9a-fA-F]{4,12}))?$", re.I)


def _fmt_job_line(job: dict) -> str:
    bits = [f"`{job['job_id']}`", job["status"]]
    if job.get("rounds_done") is not None:
        bits.append(f"round {job['rounds_done']}/{job.get('max_rounds', '?')}")
    return f"{' · '.join(bits)} — {job.get('question', '')[:90]}"


@register_tool("research")
def _research(payload: str) -> str:
    """Run a long-running research job: search the web, read pages, and summarize.

    Payload forms:
        <question>        start a new job, returns its id immediately
        status [id]       progress of a job (most recent if no id)
        result [id]       the finished report (most recent if no id)
        list              recent jobs
        cancel <id>       stop a job

    Jobs run in the background for minutes and persist in MongoDB, so they
    survive restarts and can be collected days later. A finished job also posts
    its report into the conversation on its own.
    """
    import falcon.research as Research

    text = (payload or "").strip()
    identity = current_identity()

    if not text:
        return (
            "[ERROR] research needs a question. Example:\n"
            "[AGENT: research]\nWhat are the current EU rules on AI model transparency?\n[/AGENT]"
        )

    if text.lower() == "list":
        jobs = Research.list_jobs(identity, limit=10)
        if not jobs:
            return "No research jobs yet."
        return "Recent research jobs:\n" + "\n".join(f"- {_fmt_job_line(j)}" for j in jobs)

    m = _RESEARCH_CMD.match(text)
    if m:
        action = m.group(1).lower()
        job_id = (m.group(2) or "").lower()

        if not job_id:
            recent = Research.list_jobs(identity, limit=1)
            if not recent:
                return "No research jobs yet."
            job_id = recent[0]["job_id"]

        if action == "cancel":
            ok = Research.cancel_job(job_id, identity)
            return f"Research job `{job_id}` cancelled." if ok else (
                f"[ERROR] No running job `{job_id}` to cancel."
            )

        job = Research.get_job(job_id, identity)
        if not job:
            return f"[ERROR] No research job `{job_id}`."

        if action == "status":
            lines = [
                f"Research job `{job['job_id']}` — **{job['status']}**",
                f"Question: {job['question']}",
                f"Rounds: {job['rounds_done']}/{job['max_rounds']} · "
                f"{len(job.get('findings') or [])} findings from {len(job.get('sources') or [])} sources",
                f"Search provider: {job.get('provider', 'unknown')}",
                f"Started: {job['created_at']}",
            ]
            if job.get("error"):
                lines.append(f"Error: {job['error']}")
            if job["status"] == "done":
                lines.append(f"Report ready — use [AGENT: research] result {job['job_id']} [/AGENT]")
            return "\n".join(lines)

        # action == "result"
        if job["status"] != "done":
            return (
                f"Research job `{job['job_id']}` is **{job['status']}** "
                f"({job['rounds_done']}/{job['max_rounds']} rounds). No report yet."
            )
        return f"Research report — `{job['job_id']}`\nQuestion: {job['question']}\n\n{job['report']}"

    # Anything else is a new question.
    try:
        job = Research.start_job(text, identity_id=identity)
    except ValueError as exc:
        return f"[ERROR] {exc}"

    return (
        f"Research job `{job['job_id']}` started — searching via {job['provider']}, "
        f"up to {job['max_rounds']} rounds.\n"
        f"Question: {job['question']}\n\n"
        f"This runs in the background and survives restarts. The report will be posted here when "
        f"it is ready; you can also check with [AGENT: research] status {job['job_id']} [/AGENT]."
    )


# ---------------------------------------------------------------------------
# spawn_agent — Dynamic tool generator
# ---------------------------------------------------------------------------

# Sentinel: prevents re-entrant calls while a spawn is already running.
_spawn_in_progress = False

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize_tool_name(raw: str) -> str:
    """Convert an arbitrary string into a valid snake_case tool name."""
    name = raw.strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "unnamed_tool"


def _tool_name_from_context(context: str) -> str:
    """Ask GPT-4o-mini to extract a concise snake_case tool name from context."""
    import falcon.config as Config
    from openai import OpenAI

    api_key = Config.OPENROUTER_API_KEY
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/falcon",
            "X-Title": "Falcon",
        },
    )
    try:
        resp = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract a concise snake_case tool name (max 4 words) from a description. "
                        "Return ONLY the name as plain text, nothing else. "
                        "Examples: create_mailbox, signup_twitter, send_email, fetch_weather"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Context: {context[:500]}",
                },
            ],
            temperature=0.0,
            max_tokens=20,
        )
        raw = resp.choices[0].message.content or ""
        return _sanitize_tool_name(raw.strip())
    except Exception as exc:
        logger.warning("spawn_agent: tool name extraction failed: %s", exc)
        return _sanitize_tool_name(context[:40])


def _generate_tool_code(tool_name: str, context: str) -> str:
    """Use GPT-4o-mini to generate a stub tool function for the given context.

    Returns Python source text for a decorated function, ready to be appended
    to watcher_tools.py. The stub follows the same pattern as the built-in
    [NOT CONFIGURED] stubs — it doesn't do real work but documents exactly
    what credentials / setup it would need.
    """
    import falcon.config as Config
    from openai import OpenAI

    api_key = Config.OPENROUTER_API_KEY
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/falcon",
            "X-Title": "Falcon",
        },
    )

    system_prompt = textwrap.dedent("""
        You are a Python code generator for the Falcon watcher tool registry.

        Write a single Python function that:
        1. Is decorated with @register_tool("{tool_name}") — already provided, do NOT add it.
        2. Has the signature:  def _{func_name}(payload: str) -> str:
        3. Has a one-line docstring describing what the tool is FOR.
        4. Returns a [NOT CONFIGURED] stub string that explains what credentials
           or external setup would be required to make it real, like:
               return "[NOT CONFIGURED] {tool_name} requires ..."
        5. Uses NO imports — only built-ins and the standard library.
        6. Is properly indented with 4 spaces.

        Return ONLY the function body (the def ... block), nothing else.
        No markdown, no extra text, no decorator line.
    """).strip()

    user_prompt = (
        f"Tool name: {tool_name}\n"
        f"Requested capability: {context[:800]}"
    )

    try:
        resp = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        code = resp.choices[0].message.content or ""
        # Strip markdown fences if the model wrapped in ```python ... ```
        code = re.sub(r"^```(?:python)?\n?", "", code.strip())
        code = re.sub(r"\n?```$", "", code.strip())
        return code.strip()
    except Exception as exc:
        logger.warning("spawn_agent: code generation failed: %s", exc)
        # Safe fallback stub
        return (
            f'def _{tool_name}(payload: str) -> str:\n'
            f'    """Stub for {tool_name}. Auto-generated."""\n'
            f'    return "[NOT CONFIGURED] {tool_name} — generated stub. Wire up real implementation."\n'
        )


def _update_watcher_persona_for_new_tool(tool_name: str, context: str) -> None:
    """After a new tool is registered, regenerate the watcher persona and persist it.

    This calls the watcher module's persona refresh function which in turn:
      1. Rebuilds the AVAILABLE COMMANDS block from the live tool registry.
      2. Saves the new persona text to config.yaml under `watcher_persona`.
      3. Reloads the config module so all subsequent requests see the new persona.
    """
    try:
        import falcon.watcher as Watcher
        Watcher.refresh_watcher_persona(new_tool_name=tool_name, new_tool_context=context)
    except Exception as exc:
        logger.error("spawn_agent: persona refresh failed: %s", exc)


# ---------------------------------------------------------------------------
# Public tool registration
# ---------------------------------------------------------------------------

def spawn_agent(context: str, name: str = "") -> dict:
    """Generate, register and persist a new watcher tool.

    The single implementation behind both creation paths: the watcher's
    ``[AGENT: spawn_agent …]`` marker and the REST endpoint the UI form posts
    to. Keeping them on one code path is what guarantees an agent created from
    the UI is identical to one created from chat — same validation, same
    persistence, same persona update.

    ``name`` lets a caller that already knows what the tool should be called
    skip the AI name-derivation step. Left empty, the name is inferred from
    ``context`` exactly as the chat path has always done.

    Returns ``{"status": "ok"|"error", "agent_id": str, "message": str}``.
    """
    global _spawn_in_progress

    if _spawn_in_progress:
        return {
            "status": "error",
            "agent_id": "",
            "message": "Another spawn_agent call is already in progress. Try again shortly.",
        }

    context = (context or "").strip()
    if not context:
        return {
            "status": "error",
            "agent_id": "",
            "message": "spawn_agent requires a non-empty description of the desired capability.",
        }

    _spawn_in_progress = True
    try:
        import falcon.watcher_generated as Generated

        # Make sure previously spawned tools are in the registry before we test
        # for a name collision, otherwise a fresh process would happily
        # regenerate a tool that already exists in the store.
        Generated.ensure_loaded()

        # 1. Name it. A name supplied by the caller is authoritative — someone
        #    typed it deliberately — so it is only sanitised, never handed to
        #    the model to second-guess.
        if (name or "").strip():
            tool_name = _sanitize_tool_name(name)
        else:
            tool_name = _tool_name_from_context(context)
        logger.info("spawn_agent: tool name %r for context: %s", tool_name, context[:80])

        # 2. Check if a tool with this name already exists
        if tool_name in _REGISTRY:
            return {
                "status": "error",
                "agent_id": tool_name,
                "message": f"A tool named '{tool_name}' already exists in the registry.",
            }

        # 3. Generate stub code via AI
        func_code = _generate_tool_code(tool_name, context)
        logger.info("spawn_agent: generated code for %r (%d chars)", tool_name, len(func_code))

        # 4. Reject broken generations before they reach the store — a tool that
        #    cannot compile would otherwise fail on every later load attempt.
        problem = Generated.compile_check(tool_name, func_code)
        if problem:
            logger.error("spawn_agent: generated code for %r does not compile: %s", tool_name, problem)
            return {
                "status": "error",
                "agent_id": tool_name,
                "message": f"The generated code for '{tool_name}' does not compile ({problem}). Nothing was saved.",
            }

        # 5. Load it into this process's registry first. If the function does not
        #    actually register (e.g. the model named it differently) we bail out
        #    without persisting a tool that can never be dispatched.
        if not Generated.exec_into_registry(tool_name, func_code):
            return {
                "status": "error",
                "agent_id": tool_name,
                "message": (
                    f"Tool '{tool_name}' compiled but failed to register. "
                    "Check the server logs for details. Nothing was saved."
                ),
            }

        # 6. Persist so every other process (and every restart) picks it up.
        Generated.save(tool_name, func_code, context)

        # 7. Update watcher persona so the AI knows about the new tool
        _update_watcher_persona_for_new_tool(tool_name, context)

        logger.info("spawn_agent: successfully registered tool %r", tool_name)
        return {
            "status": "ok",
            "agent_id": tool_name,
            "message": (
                f"Tool '{tool_name}' has been registered and is now available as a watcher command. "
                f"Use [AGENT: {tool_name}]...[/AGENT] to invoke it. "
                "Note: this is a stub — wire up real credentials/logic to make it functional."
            ),
        }

    except Exception as exc:
        logger.error("spawn_agent: unexpected error: %s", exc, exc_info=True)
        return {
            "status": "error",
            "agent_id": "",
            "message": f"spawn_agent failed with an unexpected error: {exc}",
        }
    finally:
        _spawn_in_progress = False


@register_tool("spawn_agent")
def _spawn_agent(payload: str) -> str:
    """Dynamically generate and register a new watcher tool from a natural-language description.

    Payload: free-form text describing the desired capability
    (e.g. "create a mailbox for the user using IMAP credentials").

    Returns JSON: {"status": "ok"|"error", "agent_id": "<tool_name>", "message": "..."}
    """
    return json.dumps(spawn_agent(payload))

