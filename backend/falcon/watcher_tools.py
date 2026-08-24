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
  post_tweet   — posts to X, but only after a human confirms. Sending text
                 stages it and returns a code; a separate `confirm <code>` in a
                 later message publishes it. Requires TWITTER_* credentials.
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


# Which message the running command was parsed out of. Used to enforce that a
# confirmation cannot arrive in the same assistant turn that requested it —
# otherwise the model could stage a tweet and confirm it in one breath, and the
# human would never see the question.
_current_msg_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "falcon_watcher_msg_id", default=""
)


def set_current_identity(identity_id: str, msg_id: str = "") -> None:
    """Record whose conversation is being served, for the duration of a dispatch."""
    _current_identity.set(identity_id or "")
    _current_msg_id.set(str(msg_id or ""))


def current_identity() -> str:
    """The identity the running tool is acting for, or "" when unknown."""
    return _current_identity.get()


def current_msg_id() -> str:
    """The message the running command came from, or "" when unknown."""
    return _current_msg_id.get()

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
    """Every tool that exists: built-ins from source, generated ones from Mongo.

    Sourced from the store rather than this process's ``_REGISTRY`` so that all
    instances give the same answer. ``ensure_loaded()`` is memoised per process,
    so a registry-based list goes stale the moment another instance spawns or
    deletes a tool — one container would advertise an agent that no longer
    exists while another omitted one that does, and which you got depended on
    load balancing.

    A tool listed here but not yet loaded locally is not a problem: ``dispatch``
    heals an unknown command from the store on first use.
    """
    import falcon.watcher_generated as Generated

    Generated.ensure_loaded()
    stored = {d["name"] for d in Generated.list_all()}
    return sorted(BUILTIN_TOOLS | stored)


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


# OAuth 1.0a user-context credentials. All four are required: posting happens as
# a user, and an app-only bearer token — the value people usually reach for —
# can read but never write.
_TWITTER_KEYS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_SECRET",
)

# Standard accounts cap at 280 characters; X Premium allows far more. Checked
# locally so an over-long tweet fails instantly with a clear message instead of
# spending an API call from a limited quota. Override if the account is Premium.
_TWEET_MAX_CHARS = 280


# Staged tweets awaiting human confirmation.
_PENDING_TWEETS = "pending_tweets"

# A staged tweet goes stale rather than being deleted: the record is kept, but
# confirming it after this long is refused, so a long-forgotten draft cannot
# fire later. Generous because the approval is a button press with the text
# visible right above it — the window guards against a stale *draft*, not a
# stale decision — and because a failed attempt (out of API credits, say) needs
# to leave enough room to fix the cause and press Post again.
_TWEET_CONFIRM_WINDOW_MINUTES = 24 * 60

# Sentinel the chat UI looks for to swap the raw result text for a card with
# Post / Reject buttons. Kept on its own line and machine-shaped so parsing it
# can never collide with tweet content.
def _confirm_marker(code: str) -> str:
    return f"[[TWEET_CONFIRM:{code}]]"


def _tweet_now(text: str) -> tuple[str, str]:
    """Actually post to X. Only ever called after a human has confirmed.

    Returns ``(outcome, message)`` where outcome is:
        "posted"  — published, message carries the URL
        "failed"  — X refused and definitely did not publish; safe to retry
        "unknown" — the request may or may not have landed (timeout, connection
                    dropped mid-flight). Never retried automatically: the tweet
                    could already be live and a retry would duplicate it.
    """
    import os

    import requests

    try:
        from requests_oauthlib import OAuth1
    except ImportError:
        return "failed", (
            "[NOT CONFIGURED] post_tweet needs the requests-oauthlib package. "
            "Add it to requirements.txt and reinstall."
        )

    auth = OAuth1(
        os.environ["TWITTER_API_KEY"].strip(),
        os.environ["TWITTER_API_SECRET"].strip(),
        os.environ["TWITTER_ACCESS_TOKEN"].strip(),
        os.environ["TWITTER_ACCESS_SECRET"].strip(),
    )

    try:
        resp = requests.post(
            "https://api.x.com/2/tweets",
            json={"text": text},
            auth=auth,
            timeout=20,
        )
    except requests.RequestException as exc:
        # The request left the machine but no response came back, so whether the
        # tweet published is genuinely unknown. Treated as terminal on purpose.
        return "unknown", (
            f"[ERROR] post_tweet could not reach X: {exc}. The tweet may or may not have "
            "been published — check the account before trying again."
        )

    if resp.status_code in (200, 201):
        tweet_id = ((resp.json() or {}).get("data") or {}).get("id", "")
        logger.info("post_tweet: posted %s (%d chars)", tweet_id, len(text))
        return "posted", f"Tweet posted: https://x.com/i/web/status/{tweet_id}"

    # X's failure modes are distinct enough that mapping them saves real
    # debugging time — a 403 here almost always means the access token was
    # generated before the app was set to Read and write.
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("title") or str(body)[:300]
    except ValueError:
        detail = resp.text[:300]

    hint = {
        401: " (credentials rejected — check all four values were copied correctly)",
        402: " (the X developer account is out of API credits — top up or upgrade the "
             "tier at developer.x.com under Products; nothing is wrong with the setup)",
        403: " (not permitted — app may be Read-only, the access token may predate "
             "the Read-and-write change, or the tweet may be a duplicate)",
        429: " (rate limited or over your API tier's posting quota)",
    }.get(resp.status_code, "")

    logger.warning("post_tweet: HTTP %s — %s", resp.status_code, detail)
    # X answered, so it definitively did not publish — the tweet stays available
    # to retry once whatever it objected to is fixed.
    return "failed", f"[ERROR] post_tweet failed: HTTP {resp.status_code}{hint} — {detail}"


def tweet_char_limit() -> int:
    """Characters allowed per tweet. Surfaced to the UI so its counter matches."""
    import os

    raw = os.environ.get("TWITTER_MAX_CHARS", "").strip()
    try:
        return int(raw) if raw else _TWEET_MAX_CHARS
    except ValueError:
        return _TWEET_MAX_CHARS


def _tweet_validation_error(text: str) -> str:
    """Everything that would make a post fail, checked before we ask the human.

    Asking "are you sure?" about a tweet that cannot be posted wastes the
    person's decision, so credentials and length are checked up front — and
    again on the way out, since the text can be edited before posting.
    """
    import os

    if not text:
        return "[ERROR] post_tweet requires the tweet text as payload."

    missing = [k for k in _TWITTER_KEYS if not os.environ.get(k, "").strip()]
    if missing:
        return f"[NOT CONFIGURED] post_tweet requires {', '.join(missing)}."

    limit = tweet_char_limit()
    if len(text) > limit:
        return (
            f"[ERROR] Tweet is {len(text)} characters; the limit is {limit}. "
            "Shorten it, or set TWITTER_MAX_CHARS if this account has X Premium."
        )
    return ""


def _pending_coll():
    from falcon.db import get_db

    return get_db()[_PENDING_TWEETS]


def get_staged_tweet(code: str, identity_id: str = "") -> dict | None:
    """One staged tweet, for the confirmation card to render its current state."""
    q: dict = {"code": code.strip().lower()}
    if identity_id:
        q["identity_id"] = identity_id
    return _pending_coll().find_one(q, {"_id": 0})


def edit_tweet(code: str, text: str, identity_id: str = "") -> tuple[bool, str]:
    """Replace the text of a staged tweet. Returns (ok, message)."""
    from datetime import datetime, timezone

    text = (text or "").strip()
    problem = _tweet_validation_error(text)
    if problem:
        return False, problem.replace("[ERROR] ", "").replace("[NOT CONFIGURED] ", "")

    q: dict = {"code": code.strip().lower(), "status": "pending"}
    if identity_id:
        q["identity_id"] = identity_id
    res = _pending_coll().update_one(
        q,
        {"$set": {"text": text, "edited_at": datetime.now(timezone.utc), "edited": True}},
    )
    if res.matched_count == 0:
        return False, f"No pending tweet with code '{code}'."
    return True, "Draft updated."


def confirm_tweet(code: str, identity_id: str = "", text: str | None = None) -> tuple[bool, str]:
    """Publish a staged tweet. Returns (ok, message).

    Reachable only from the authenticated REST endpoint behind the Post button —
    deliberately not from the tool registry, so no model output can ever reach
    it. The human clicking is the authorisation.

    ``text`` carries whatever the user had on screen when they pressed Post. It
    is written to the record before publishing, so the stored tweet is always
    the tweet that actually went out — never the model's original draft that the
    user had since edited away.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    coll = _pending_coll()
    q: dict = {"code": code.strip().lower()}
    if identity_id:
        q["identity_id"] = identity_id
    doc = coll.find_one(q)

    if not doc:
        return False, f"No staged tweet with code '{code}'."
    if doc.get("status") != "pending":
        return False, f"This tweet was already {doc['status']}."

    if text is not None and text.strip() != doc.get("text", ""):
        ok, message = edit_tweet(code, text, identity_id)
        if not ok:
            return False, message
        doc = coll.find_one(q)

    staged_at = doc.get("staged_at")
    if isinstance(staged_at, datetime):
        if staged_at.tzinfo is None:
            staged_at = staged_at.replace(tzinfo=timezone.utc)
        if now - staged_at > timedelta(minutes=_TWEET_CONFIRM_WINDOW_MINUTES):
            coll.update_one({"_id": doc["_id"]}, {"$set": {"status": "expired", "resolved_at": now}})
            return False, (
                f"This tweet was staged more than {_TWEET_CONFIRM_WINDOW_MINUTES} minutes ago "
                "and has expired. Ask for it again."
            )

    outcome, message = _tweet_now(doc["text"])

    if outcome == "failed":
        # X answered and refused, so nothing was published. Keeping the tweet
        # pending means the user can fix the cause — credits, permissions, rate
        # limit — and press Post again on the same text, instead of having to
        # get the agent to compose it a second time.
        coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {"result": message[:500], "last_error_at": now},
             "$inc": {"attempts": 1}},
        )
    else:
        coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "status": "posted" if outcome == "posted" else "failed",
                "resolved_at": now,
                "result": message[:500],
            }},
        )

    logger.info("post_tweet: %s confirmed by user — %s", code, outcome)
    return outcome == "posted", message


def cancel_tweet(code: str, identity_id: str = "") -> tuple[bool, str]:
    """Discard a staged tweet without posting. Returns (ok, message)."""
    from datetime import datetime, timezone

    q: dict = {"code": code.strip().lower(), "status": "pending"}
    if identity_id:
        q["identity_id"] = identity_id
    res = _pending_coll().update_one(
        q, {"$set": {"status": "cancelled", "resolved_at": datetime.now(timezone.utc)}}
    )
    if res.modified_count == 0:
        return False, f"No pending tweet with code '{code}'."
    logger.info("post_tweet: %s rejected by user", code)
    return True, "Tweet discarded — nothing was posted."


@register_tool("post_tweet")
def _post_tweet(payload: str) -> str:
    """Stage a tweet for the user to approve. Never posts.

    This tool has exactly one effect: it writes the proposed text to
    ``pending_tweets`` and returns a confirmation code. Publishing happens only
    when a signed-in human clicks Post in the chat UI, which calls a REST
    endpoint the tool registry cannot reach. There is deliberately no way to
    post from a model-emitted command.

    Payload: the tweet text.
    """
    import secrets
    from datetime import datetime, timezone

    text = (payload or "").strip()

    # Checked before staging so the user is never asked to approve something
    # that could not have been posted anyway.
    problem = _tweet_validation_error(text)
    if problem:
        return problem

    code = secrets.token_hex(2)
    _pending_coll().insert_one({
        "code": code,
        "identity_id": current_identity(),
        "origin_msg_id": current_msg_id(),
        "text": text,
        "status": "pending",
        "staged_at": datetime.now(timezone.utc),
        "resolved_at": None,
        "result": "",
    })
    logger.info("post_tweet: staged %r awaiting user approval (%d chars)", code, len(text))

    # The marker is what the chat UI swaps for the Post / Reject buttons. The
    # plain text above it is the fallback if an older client renders this
    # message without understanding the marker.
    return (
        f"NOT POSTED — waiting for your approval.\n\n{text}\n\n"
        f"{_confirm_marker(code)}"
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

        # 2. Check if a tool with this name already exists. The store is checked
        #    as well as the local registry: a tool spawned by another instance
        #    is not in this process's registry, and without this the save below
        #    would silently overwrite it.
        if tool_name in _REGISTRY or Generated.get(tool_name) is not None:
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


# ---------------------------------------------------------------------------
# Built-in snapshot — must stay the last statement in this module
# ---------------------------------------------------------------------------

# Every tool defined above, captured before any generated tool can be exec'd
# into the registry. That makes it the authoritative "defined in source" set,
# which list_tools() needs in order to tell a built-in apart from a generated
# tool that another process has since deleted from the store.
#
# Taken at import time on purpose: adding a @register_tool above updates this
# automatically, so it cannot fall out of step with the code the way a
# hand-maintained list would.
BUILTIN_TOOLS: frozenset[str] = frozenset(_REGISTRY)

