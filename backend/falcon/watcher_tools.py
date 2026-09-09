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
  fetch_replies— reads the thread under a post on X, given its URL or id.
                 Read-only. Uses TWITTER_BEARER_TOKEN if set, otherwise the same
                 TWITTER_* OAuth values as post_tweet.
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
from typing import Any, Callable

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

# Tools that destroy something when they run. Not a permission — the watcher
# dispatches these exactly as it does any other tool — but the Watcher Agents
# tab, where a person presses Run with no model in between, asks for a second
# press before dispatching one of these. Nothing here can be undone afterwards.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({"delete_doc"})


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


# ---------------------------------------------------------------------------
# fetch_replies — read the conversation under a post
# ---------------------------------------------------------------------------

# X has no "get replies" endpoint. Replies are found by searching for posts that
# share the thread's conversation_id, which every post in a thread carries and
# which equals the id of the post that started it.
#
# Two consequences shape everything below:
#   * Recent search only reaches back 7 days. Replies to an older post are not
#     missing — they are unreachable without full-archive access (Pro tier up).
#   * The search returns the whole thread, replies-to-replies included, not just
#     the posts answering the one that was asked about.

# Accepts what a person actually pastes: a status URL from x.com or twitter.com
# with any trailing query string, the /i/web/status/ form post_tweet returns, or
# a bare id. Matched strictly rather than by scanning for digits, so a number in
# a username can never be mistaken for the post id.
_STATUS_URL_RE = re.compile(
    r"(?:twitter|x)\.com/(?:[^/\s?#]+|i/web)/status(?:es)?/(\d{5,25})", re.I
)
_BARE_ID_RE = re.compile(r"^\d{5,25}$")

# An optional trailing "limit N", so a big thread can be opened up deliberately
# rather than by default.
_REPLY_LIMIT_RE = re.compile(r"\s+limit\s+(\d{1,3})\s*$", re.I)

# Reads are billed per post on X's pay-per-use pricing, so an unbounded fetch on
# a viral thread is a real bill rather than just a slow call. The default is
# meant to answer "what did people say?" in one page; larger needs an explicit
# limit, and even that is bounded.
_DEFAULT_REPLY_LIMIT = 50
_MAX_REPLY_LIMIT = 200

# X rejects max_results below 10, so a smaller limit still costs a full page.
_MIN_PAGE = 10


def _x_read_auth() -> tuple[dict, object | None, str]:
    """Credentials for X read endpoints, as ``(headers, auth, error)``.

    Prefers the app-only bearer token when one is set: it is the credential X
    documents for search, and it draws on a rate-limit pool separate from the
    user-context tokens post_tweet spends. Falls back to the OAuth 1.0a keys so
    reading works with what is already configured, without requiring a fifth
    secret before the tool does anything at all.
    """
    import os

    bearer = os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}, None, ""

    missing = [k for k in _TWITTER_KEYS if not os.environ.get(k, "").strip()]
    if missing:
        return {}, None, (
            "[NOT CONFIGURED] fetch_replies needs either TWITTER_BEARER_TOKEN or all "
            f"four OAuth values. Missing: {', '.join(missing)}."
        )

    try:
        from requests_oauthlib import OAuth1
    except ImportError:
        return {}, None, (
            "[NOT CONFIGURED] fetch_replies needs the requests-oauthlib package. "
            "Add it to requirements.txt and reinstall."
        )

    return {}, OAuth1(
        os.environ["TWITTER_API_KEY"].strip(),
        os.environ["TWITTER_API_SECRET"].strip(),
        os.environ["TWITTER_ACCESS_TOKEN"].strip(),
        os.environ["TWITTER_ACCESS_SECRET"].strip(),
    ), ""


def _x_get(path: str, params: dict, headers: dict, auth) -> tuple[dict | None, str]:
    """One GET against the X API. Returns ``(json, error)`` — exactly one is set."""
    import requests

    try:
        resp = requests.get(
            f"https://api.x.com{path}",
            params=params,
            headers=headers,
            auth=auth,
            timeout=20,
        )
    except requests.RequestException as exc:
        return None, f"[ERROR] fetch_replies could not reach X: {exc}"

    if resp.status_code == 200:
        try:
            return resp.json() or {}, ""
        except ValueError:
            return None, "[ERROR] fetch_replies got a non-JSON response from X."

    try:
        body = resp.json()
        detail = body.get("detail") or body.get("title") or str(body)[:300]
    except ValueError:
        detail = resp.text[:300]

    # Same reasoning as post_tweet: these codes mean genuinely different things
    # and guessing wrong costs an afternoon.
    hint = {
        401: " (credentials rejected — check the bearer token, or all four OAuth values)",
        402: " (out of API credits — reads are billed per post retrieved; top up at "
             "developer.x.com under Products)",
        403: " (not permitted — search access is not included in this API tier)",
        404: " (no such post — it may have been deleted, or its author may be protected)",
        429: " (rate limited — wait for the window to reset before retrying)",
    }.get(resp.status_code, "")

    logger.warning("fetch_replies: HTTP %s — %s", resp.status_code, detail)
    return None, f"[ERROR] fetch_replies failed: HTTP {resp.status_code}{hint} — {detail}"


def _resolve_conversation(tweet_id: str, headers: dict, auth) -> tuple[dict, dict, str]:
    """Look up one post, returning ``(post, author, error)``.

    Costs one extra read, and buys two things worth more than it. A link pasted
    from the middle of a thread is a *reply*, whose own id is not the
    conversation id — searching on it would quietly return nothing. And the root
    post's id and author are what distinguish a direct reply from a reply three
    levels down.
    """
    data, err = _x_get(
        f"/2/tweets/{tweet_id}",
        {
            # public_metrics is free here and is what makes an empty search
            # readable: X reports how many replies exist, so "none returned" can
            # be told apart from "none exist".
            "tweet.fields": "conversation_id,author_id,created_at,text,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,name",
        },
        headers,
        auth,
    )
    if err:
        return {}, {}, err

    post = data.get("data") or {}
    if not post:
        return {}, {}, (
            f"[ERROR] fetch_replies: X returned no post for id {tweet_id}. It may have "
            "been deleted, or belong to a protected account."
        )

    users = {u["id"]: u for u in (data.get("includes") or {}).get("users") or []}
    return post, users.get(post.get("author_id", ""), {}), ""


def _search_conversation(
    conversation_id: str, limit: int, headers: dict, auth
) -> tuple[list, dict, str, str]:
    """Page through a conversation. Returns ``(posts, users, error, warning)``.

    ``error`` is set only when nothing at all was retrieved. A page failing after
    some results are already in hand becomes a warning instead: the replies we
    paid for are still worth showing, and re-running to chase the rest would
    re-buy the ones we already have.
    """
    posts: list[dict] = []
    users: dict[str, dict] = {}
    token = ""

    while len(posts) < limit:
        params = {
            "query": f"conversation_id:{conversation_id}",
            "max_results": max(_MIN_PAGE, min(100, limit - len(posts))),
            "tweet.fields": "author_id,created_at,in_reply_to_user_id,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        if token:
            params["next_token"] = token

        data, err = _x_get("/2/tweets/search/recent", params, headers, auth)
        if err:
            if posts:
                return posts, users, "", f"Stopped early — {err}"
            return [], {}, err, ""

        for user in (data.get("includes") or {}).get("users") or []:
            users[user["id"]] = user
        posts.extend(data.get("data") or [])

        token = (data.get("meta") or {}).get("next_token") or ""
        if not token:
            break

    return posts[:limit], users, "", ""


def _handle(user: dict, author_id: str) -> str:
    return "@" + user.get("username", "") if user.get("username") else f"user {author_id}"


def _is_direct_reply(post: dict, root_id: str) -> bool:
    """True when this post answers the root itself, not another reply."""
    for ref in post.get("referenced_tweets") or []:
        if ref.get("type") == "replied_to":
            return str(ref.get("id")) == root_id
    return False


def _post_age_days(root: dict) -> float | None:
    """How old the post is, or None if X did not give a parseable timestamp."""
    from datetime import datetime, timezone

    raw = (root.get("created_at") or "").replace("Z", "+00:00")
    if not raw:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds() / 86400
    except ValueError:
        return None


def _explain_empty(root: dict) -> str:
    """Say *why* nothing came back, which is rarely 'nobody replied'.

    X reports a reply_count on the post itself, so an empty search can be
    diagnosed rather than guessed at. Without this the tool says "no replies
    found" about a post X has just told us has replies — technically true,
    completely misleading, and the model would repeat it to the user as fact.
    """
    stated = (root.get("public_metrics") or {}).get("reply_count")
    age = _post_age_days(root)

    if stated == 0:
        return "This post has no replies — X reports a reply count of 0."

    if stated:
        plural = "reply" if stated == 1 else "replies"
        if age is not None and age > 7:
            return (
                f"X reports {stated} {plural} on this post, but none could be retrieved: "
                f"the post is {age:.0f} days old and recent search only covers the last 7 "
                "days. Reading them would need full-archive search, which is not included "
                "below X's Pro tier."
            )
        return (
            f"X reports {stated} {plural} on this post, but the search returned none. "
            "They may have been deleted, or come from protected or suspended accounts, "
            "which are excluded from search results."
        )

    # No metrics to go on — fall back to naming the limitation.
    if age is not None and age > 7:
        return (
            f"No replies found, but this post is {age:.0f} days old and recent search only "
            "covers the last 7 days — so this does not mean it has none."
        )
    return (
        "No replies found. Note that recent search only covers the last 7 days, so an "
        "older post can show none even when it has them."
    )


def _shortfall_note(root: dict, direct: int, limit: int, hit_limit: bool) -> str:
    """Flag replies X says exist but that search did not return."""
    stated = (root.get("public_metrics") or {}).get("reply_count")
    if hit_limit or not stated or direct >= stated:
        return ""
    age = _post_age_days(root)
    why = (
        f" — the post is {age:.0f} days old and search only reaches back 7"
        if age is not None and age > 7
        else " (deleted, protected or suspended authors are excluded from search)"
    )
    return f"X reports {stated} replies; {direct} were retrievable{why}."


def _format_replies(
    root: dict, root_author: dict, posts: list, users: dict, limit: int, warning: str
) -> str:
    root_id = str(root.get("id", ""))
    root_handle = _handle(root_author, root.get("author_id", ""))

    # The root shares the conversation id, so it comes back in its own search.
    replies = [p for p in posts if str(p.get("id")) != root_id]
    # Search returns newest first; a conversation reads better oldest first.
    replies.reverse()

    header = f"**Replies to {root_handle}** — https://x.com/i/web/status/{root_id}"
    quoted = " ".join((root.get("text") or "").split())
    if quoted:
        header += f"\n\n> {quoted[:200]}{'…' if len(quoted) > 200 else ''}"

    if not replies:
        return f"{header}\n\n{_explain_empty(root)}"

    # A blank line between entries so each reply is its own paragraph in the
    # chat; without it markdown runs the whole thread into one block of text.
    lines = []
    for i, post in enumerate(replies, start=1):
        author = users.get(post.get("author_id", ""), {})
        when = (post.get("created_at") or "")[:16].replace("T", " ")
        kind = " · _further down the thread_" if not _is_direct_reply(post, root_id) else ""
        text = " ".join((post.get("text") or "").split())
        lines.append(
            f"{i}. **{_handle(author, post.get('author_id', ''))}** · {when}{kind}\n\n"
            f"   {text}"
        )

    direct = sum(1 for p in replies if _is_direct_reply(p, root_id))
    hit_limit = len(replies) >= limit
    summary = f"{len(replies)} post{'s' if len(replies) != 1 else ''} in the thread ({direct} direct)"
    if hit_limit:
        summary += f" — stopped at the limit of {limit}; add 'limit N' for more"

    out = f"{header}\n\n{summary}\n\n" + "\n\n".join(lines)
    shortfall = _shortfall_note(root, direct, limit, hit_limit)
    if shortfall:
        out += f"\n\n**[NOTE]** {shortfall}"
    if warning:
        out += f"\n\n**[WARNING]** {warning}"
    return out


@register_tool("fetch_replies")
def _fetch_replies(payload: str) -> str:
    """Read the replies under a post on X.

    Payload: a post URL or bare id, optionally followed by ``limit N``.
    Read-only — this tool never writes to X.
    """
    raw = (payload or "").strip()
    if not raw:
        return "[ERROR] fetch_replies requires a post URL or id as payload."

    limit = _DEFAULT_REPLY_LIMIT
    match = _REPLY_LIMIT_RE.search(raw)
    if match:
        limit = max(1, min(_MAX_REPLY_LIMIT, int(match.group(1))))
        raw = raw[: match.start()].strip()

    found = _STATUS_URL_RE.search(raw)
    tweet_id = found.group(1) if found else (raw if _BARE_ID_RE.match(raw) else "")
    if not tweet_id:
        return (
            "[ERROR] fetch_replies could not find a post id in that payload. Give it a "
            "link like https://x.com/user/status/1234567890, or the numeric id on its own."
        )

    headers, auth, err = _x_read_auth()
    if err:
        return err

    root, root_author, err = _resolve_conversation(tweet_id, headers, auth)
    if err:
        return err

    # Falls back to the id itself, which is correct whenever the link points at
    # the start of a thread — the common case.
    conversation_id = str(root.get("conversation_id") or tweet_id)

    posts, users, err, warning = _search_conversation(conversation_id, limit, headers, auth)
    if err:
        return err

    logger.info(
        "fetch_replies: conversation %s → %d posts (limit %d)",
        conversation_id, len(posts), limit,
    )
    return _format_replies(root, root_author, posts, users, limit, warning)


@register_tool("persistent_memory_access_bridge")
def _persistent_memory_access_bridge(payload: str) -> str:
    """Prove the database really reads and writes, and report the real result.

    Runs an actual round-trip against the live database — write, read back,
    compare, update, re-read — plus a durability check for probes left by
    earlier processes. Returns whichever verdict is true; it has no success
    path that does not depend on the round-trip having actually worked.

    Payload is ignored, so any phrasing the model reaches for still runs the
    same probe.
    """
    from falcon import memory_bridge

    try:
        return memory_bridge.format_report(memory_bridge.check())
    except Exception as exc:  # noqa: BLE001
        # check() catches its own step failures, so reaching here means the
        # module could not run at all — still a real answer, not a claim.
        logger.error("persistent_memory_access_bridge: probe could not run: %s", exc)
        return (
            f"MEMORY BRIDGE: FAILED — the probe could not run at all: "
            f"{type(exc).__name__}: {exc}\n\n"
            "Storage is NOT confirmed working. Do not claim anything was remembered, "
            "saved or recalled."
        )


@register_tool("memory_status")
def _memory_status(payload: str) -> str:
    """Report storage status: configured, writable, and when it last wrote.

    "Configured" is answered from the environment, but "writable" is not taken
    on trust — it runs the bridge probe, because a set connection string proves
    only that someone typed one.
    """
    import os

    from falcon import documents_store as Store
    from falcon import memory_bridge

    configured = bool(os.environ.get("MONGODB_URI", "").strip())
    lines = ["**MEMORY STATUS**", ""]
    lines.append(f"- **Configured:** {'yes' if configured else '**NO** — MONGODB_URI is not set'}")

    if not configured:
        lines += [
            "- **Writable:** no (cannot connect without a connection string)",
            "- **Last write:** unknown",
            "",
            "Storage is NOT working. Do not claim anything was saved or remembered.",
        ]
        return "\n".join(lines)

    res = memory_bridge.check()
    if res["ok"]:
        dur = res.get("durability") or {}
        proven = " (persistence proven across restarts)" if dur.get("survived_restart") else \
                 " (first probe on this database — durability not yet demonstrable)"
        lines.append(f"- **Writable:** yes — verified round-trip in {res['total_ms']} ms{proven}")
    else:
        lines.append(f"- **Writable:** **NO** — failed at '{res['failed_step']}': {res['error']}")

    last = Store.last_write()
    if last:
        when = last.get("at")
        when_s = when.strftime("%Y-%m-%d %H:%M UTC") if hasattr(when, "strftime") else str(when)
        lines.append(
            f"- **Last write:** {when_s} — {_md(last.get('filename', '?'))} "
            f"(`{last.get('storage_id', '?')}`)"
        )
    else:
        lines.append("- **Last write:** none recorded yet")

    try:
        # Scoped to the caller. An unscoped count reported the whole cluster's
        # document total to whoever asked, which told one account how much other
        # accounts had stored.
        st = Store.stats(current_identity())
        by = ", ".join(f"{k}={v}" for k, v in sorted(st["by_source"].items())) or "none"
        lines.append(f"- **Documents:** {st['total']} stored ({by})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- **Documents:** could not count ({exc})")

    if not res["ok"]:
        lines += ["", "Storage is NOT confirmed working. Do not claim anything was saved."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# library_store — write text into the durable library with title and tags
# ---------------------------------------------------------------------------

# Metadata headers accepted at the top of a library_store payload. Several
# spellings for each because the model reaches for whichever word fits its
# sentence, and rejecting "Name:" in favour of "Title:" would fail a save for a
# reason that has nothing to do with storage.
_LIB_TITLE_KEYS = {"title", "name", "subject"}
_LIB_TAGS_KEYS = {"tags", "tag", "keywords", "labels"}
_LIB_BODY_KEYS = {"body", "text", "content", "document"}
_LIB_HEADER_RE = re.compile(r"^\s*([A-Za-z_]{3,12})\s*[:=]\s*(.*)$")
# A rule of dashes, equals or underscores — the conventional "metadata ends
# here" marker, and the one thing that lets a body legitimately begin with
# something that looks like a header.
_LIB_SEP_RE = re.compile(r"^\s*(?:-{3,}|={3,}|_{3,}|\*{3,})\s*$")
_LIB_TITLE_FROM_BODY = 80


def _parse_library_payload(payload: str) -> tuple[str, list[str], str, str]:
    """Split a library_store payload into (title, tags, body, error).

    Accepts three shapes, because all three are things a model actually emits:

      JSON      {"title": ..., "tags": [...], "body": ...}
      Headers   Title: ...\\nTags: ...\\n---\\n<body>
      Bare text the whole payload is the body and the first line becomes the title

    Only ``Title``/``Tags``/``Body`` and their synonyms are consumed as headers,
    and only in the run of lines at the very top. Anything else ends the header
    block and starts the body, so a document whose first line happens to read
    "Note: see chapter 4" keeps that line instead of losing it to a parser.
    """
    raw = (payload or "").strip()
    if not raw:
        return "", [], "", "library_store needs something to store — the payload was empty."

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            lowered = {str(k).strip().lower(): v for k, v in data.items()}
            title = ""
            for key in ("title", "name", "subject"):
                if lowered.get(key):
                    title = str(lowered[key]).strip()
                    break
            tags: Any = None
            for key in ("tags", "tag", "keywords", "labels"):
                if lowered.get(key) is not None:
                    tags = lowered[key]
                    break
            body = ""
            for key in ("body", "text", "content", "document"):
                if lowered.get(key):
                    body = str(lowered[key]).strip()
                    break
            if body:
                from falcon import documents_store as Store

                return title, Store.normalize_tags(tags), body, ""
            # A JSON object with no body field is more likely a document that
            # merely starts with a brace than a malformed command, so fall
            # through to the text path rather than failing.

    title = ""
    tags_raw = ""
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _LIB_SEP_RE.match(line):
            i += 1
            break
        m = _LIB_HEADER_RE.match(line)
        if not m:
            break
        key, value = m.group(1).strip().lower(), m.group(2).strip()
        if key in _LIB_TITLE_KEYS:
            title = title or value
        elif key in _LIB_TAGS_KEYS:
            tags_raw = tags_raw or value
        elif key in _LIB_BODY_KEYS:
            # Body may start on this line or on the next.
            rest = "\n".join([value] + lines[i + 1:])
            i = len(lines)
            body = rest.strip()
            from falcon import documents_store as Store

            if not body:
                return "", [], "", "the payload had headers but no body text to store."
            return title, Store.normalize_tags(tags_raw), body, ""
        else:
            break
        i += 1

    body = "\n".join(lines[i:]).strip()
    if not body:
        return "", [], "", (
            "the payload had headers but no body text to store. Put the full text "
            "after the headers, separated by a line of dashes."
        )

    if not title:
        # First body line, cleaned of markdown heading syntax. A derived title is
        # better than "untitled" and the caller can always store an explicit one.
        first = body.splitlines()[0].strip().lstrip("#").strip().strip("*_`").strip()
        title = (first[:_LIB_TITLE_FROM_BODY].rstrip() + "…") if len(first) > _LIB_TITLE_FROM_BODY else first

    from falcon import documents_store as Store

    return title, Store.normalize_tags(tags_raw), body, ""


@register_tool("library_store")
def _library_store(payload: str) -> str:
    """Store text in the durable library with a title and tags.

    Payload accepts headers, JSON, or bare text::

        Title: Chapter Three — The Descent
        Tags: manuscript, draft, act-two
        ---
        <the full body text>

    Returns the storage id only when the write has been read back and verified,
    so a returned id always means the text is genuinely retrievable. A failure
    says so plainly and hands back no id, because a false confirmation is the
    one outcome the user cannot check for themselves.
    """
    from falcon import documents_store as Store

    title, tags, body, error = _parse_library_payload(payload)
    if error:
        return f"[ERROR] NOT STORED — {error}"

    identity = current_identity()
    try:
        res = Store.save(
            identity,
            title,
            body,
            source="library",
            kind="library",
            title=title,
            tags=tags,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("library_store: save raised for %r: %s", title, exc)
        return (
            f"[ERROR] NOT STORED — the write failed: {type(exc).__name__}: {exc}\n"
            "Nothing was saved. Do not tell the user this was stored."
        )

    if not res.get("ok"):
        return (
            f"[ERROR] NOT STORED — {res.get('error') or 'unknown storage failure'}\n"
            "Nothing was saved. Do not tell the user this was stored."
        )

    when = res.get("saved_at")
    when_s = when.strftime("%Y-%m-%d %H:%M UTC") if hasattr(when, "strftime") else str(when)
    stored_tags = res.get("tags") or tags
    lines = [
        "**STORED** — written and read back, verified." if not res.get("duplicate")
        else "**ALREADY STORED** — identical text was already in the library.",
        "",
        f"- **Storage id:** `{res['storage_id']}`",
        f"- **Title:** {_md(res.get('title') or title)}",
        f"- **Tags:** {', '.join(stored_tags) if stored_tags else '—'}",
        f"- **Size:** {res.get('chars', len(body)):,} chars",
        f"- **Saved:** {when_s}",
    ]
    if res.get("truncated"):
        lines.append(
            f"- **Note:** the text was longer than {Store.MAX_TEXT_CHARS:,} characters "
            "and was truncated to fit."
        )
    if res.get("metadata_updated"):
        lines.append("- **Note:** the title and tags on the existing entry were updated.")
    lines += [
        "",
        f"Read it back at any time with `read_document {res['storage_id']}`.",
    ]
    return "\n".join(lines)


@register_tool("list_documents")
def _list_documents(payload: str) -> str:
    """List stored documents, or search them when given a term.

    Payload: empty to list recent documents, or a search term.
    """
    from falcon import documents_store as Store

    term = (payload or "").strip()
    identity = current_identity()
    docs = Store.search(identity, term) if term else Store.list_documents(identity)

    if not docs:
        if term:
            return f"No stored documents match {term!r}."
        return (
            "No documents are stored for this identity. Note that documents uploaded "
            "before durable storage was added were never saved — use the backfill to "
            "recover any that are still in the audit log."
        )

    n = len(docs)
    header = f"**{n} document{'s' if n != 1 else ''}"
    header += f" matching “{term}”**" if term else "**"

    lines = [
        header,
        "",
        "| Document | Storage ID | Tags | Size | Kind | Saved |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for d in docs:
        when = d.get("saved_at")
        when_s = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)[:10]
        tags = d.get("tags") or []
        # "file" means the original is downloadable; "text only" means all that
        # survives is the extraction, so no download can be offered for it.
        kind = "file" if d.get("file_id") else "text only"
        lines.append(
            f"| {_md(Store.display_name(d))} | `{d['storage_id']}` "
            f"| {_md(', '.join(tags)) if tags else '—'} | {d.get('chars', 0):,} chars "
            f"| {kind} | {when_s} |"
        )
    lines += [
        "",
        "Use `read_document` with a storage id to get one back. Entries marked **file** can be "
        "handed to the user as a download; **text only** entries cannot.",
    ]
    return "\n".join(lines)


# How much of an uploaded file to show: enough to confirm it is the right
# document, no more. The file itself is the deliverable — it goes back as a
# download, not as thousands of characters printed into the chat. Free text has
# no file to hand over, so it is returned whole instead.
_DOC_PREVIEW_LINES = 6
_DOC_PREVIEW_CHARS = 400


def _first_lines(text: str, lines: int = _DOC_PREVIEW_LINES, chars: int = _DOC_PREVIEW_CHARS) -> str:
    """The opening few lines, whichever limit is reached first.

    Blank lines are dropped before counting: an extracted PDF is full of them,
    and six lines of mostly whitespace identifies nothing.
    """
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(kept) >= lines or used + len(line) > chars:
            break
        kept.append(line)
        used += len(line)
    if not kept:  # one very long unbroken line
        return text[:chars].rstrip()
    return "\n".join(kept)


def _md(value: str) -> str:
    """Make a value safe to drop into a markdown table cell.

    Tool results are rendered as markdown in the chat, so a filename containing a
    pipe would silently split a row into the wrong columns, and one containing a
    newline would end the table early.
    """
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()


def _fence(text: str) -> str:
    """Wrap document text so it displays as written and cannot restyle the chat.

    An uploaded document is full of things markdown treats as syntax — hashes,
    asterisks, numbered lines, tables. Rendered raw it reflows into headings and
    lists and stops looking like the document. The fence is chosen longer than
    any run of backticks inside, so a document containing code cannot break out.
    """
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    bar = "`" * max(3, longest + 1)
    return f"{bar}text\n{text}\n{bar}"


def _download_url(storage_id: str, identity: str) -> str:
    """A path the chat UI turns into a working download link.

    Relative on purpose: in production the frontend and the API share an origin,
    and in development the client prefixes it with the API base it already knows.
    Hard-coding a host here would be wrong in one of those two places.
    """
    from urllib.parse import quote

    url = f"/api/documents/stored/{storage_id}/download"
    return f"{url}?identity_id={quote(identity)}" if identity else url


@register_tool("read_document")
def _read_document(payload: str) -> str:
    """Return a stored document as a downloadable file, with a short preview.

    Payload: the storage id, optionally followed by ``full``.

    The default answer is the file itself — a download link and its metadata —
    because someone who uploaded a PDF and asks for it back wants the PDF, not
    the text scraped out of it. Add ``full`` to get the entire extracted text.

    This result is written for a person to read. Unlike every other tool here it
    is shown in the chat and withheld from the model
    (``falcon.agent_redact.READER_FACING_COMMANDS``), because a document can be
    larger than the context window — so there is no point steering the model
    with prose it will never see, and any such line would be read by the user as
    instructions aimed at somebody else.
    """
    from falcon import documents_store as Store

    parts = (payload or "").strip().split()
    if not parts:
        return "[ERROR] read_document requires a storage id (e.g. doc_a1b2c3d4e5f6)."

    storage_id = parts[0]
    want_full = any(p.lower() in ("full", "text", "all", "--full") for p in parts[1:])

    identity = current_identity()
    doc = Store.get(storage_id, identity)
    if not doc:
        return (
            f"[ERROR] No stored document with id {storage_id!r} for this identity. "
            "Use list_documents to see what is available."
        )

    when = doc.get("saved_at")
    when_s = when.strftime("%Y-%m-%d %H:%M UTC") if hasattr(when, "strftime") else str(when)
    tags = doc.get("tags") or []
    text = doc.get("text", "")
    name = Store.display_name(doc)

    lines = [f"### {name}", ""]
    lines.append(f"- **Storage id:** `{doc['storage_id']}`")
    if tags:
        lines.append(f"- **Tags:** {', '.join(tags)}")
    lines.append(f"- **Saved:** {when_s}")

    has_file = bool(doc.get("file_id"))
    if has_file:
        size = doc.get("bytes", 0)
        pretty = f"{size / 1024:,.0f} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"
        lines.append(f"- **File:** {doc.get('content_type') or 'file'}, {pretty}")
        lines.append(f"- **Download:** [{_md(name)}]({_download_url(storage_id, identity)})")
    else:
        lines.append(f"- **File:** none — stored as text only ({doc.get('chars', 0):,} chars)")

    if doc.get("truncated"):
        lines += [
            "",
            "**Note:** the stored text was truncated at the extraction limit, so it is not the "
            + ("complete document. The downloadable file is complete."
               if has_file else "complete document."),
        ]

    # Free text — a library entry, notes, a draft — has no file to hand over, so
    # the text IS the document and comes back whole. An uploaded file is the
    # opposite: the download is the document, and its extracted text is only a
    # way to identify and search it, so it is shown a few lines at a time.
    if not has_file:
        lines += ["", f"**Full text** — {doc.get('chars', 0):,} characters", "", _fence(text)]
        return "\n".join(lines)

    if want_full:
        lines += [
            "",
            f"**Full text** — {doc.get('chars', 0):,} characters, extracted from the file",
            "",
            _fence(text),
        ]
        return "\n".join(lines)

    preview = _first_lines(text)
    lines += [
        "",
        f"**Opening lines** — of {doc.get('chars', 0):,} extracted characters",
        "",
        _fence(preview),
    ]
    if len(text) > len(preview):
        lines += [
            "",
            "That is the opening of the file, not the whole of it. Download it above for the "
            "original, or ask for the full extracted text if you want the rest here.",
        ]
    return "\n".join(lines)


# A storage id is the only thing this tool accepts. Matching the shape rather
# than taking the payload whole means "delete everything" or a title typed by
# mistake is refused instead of being interpreted as an id that happens not to
# exist — the model gets told what it did wrong.
_DOC_ID_RE = re.compile(r"\bdoc_[0-9a-f]{12}\b", re.I)


@register_tool("delete_doc")
def _delete_doc(payload: str) -> str:
    """Permanently delete a stored document, its original file and its listing.

    Payload: one storage id, or several separated by spaces or commas.

    This is the only tool that destroys stored content, and there is no undo —
    the record goes from the collection and the bytes go from GridFS. So it
    reports what it removed by title rather than only by id (the user should be
    able to see whether the right thing went), and it verifies the record is
    actually gone afterwards rather than trusting the delete call, for the same
    reason library_store reads its writes back.
    """
    from falcon import documents_store as Store

    raw = (payload or "").strip()
    if not raw:
        return (
            "[ERROR] delete_doc requires a storage id (e.g. doc_a1b2c3d4e5f6). "
            "Use list_documents to find it."
        )

    ids: list[str] = []
    for found in _DOC_ID_RE.findall(raw):
        found = found.lower()
        if found not in ids:  # the same id twice is one deletion, not a failure
            ids.append(found)

    if not ids:
        return (
            f"[ERROR] NOT DELETED — no storage id in {raw!r}. This command takes ids of the "
            "form doc_a1b2c3d4e5f6, not titles, filenames or words like 'all'. Run "
            "list_documents to get the id of the document the user means, and delete only "
            "the one they asked for."
        )

    identity = current_identity()
    done: list[str] = []
    missing: list[str] = []
    failed: list[str] = []

    for storage_id in ids:
        # Read it before it goes: afterwards there is nothing left to name it by.
        doc = Store.get(storage_id, identity)
        if not doc:
            missing.append(storage_id)
            continue

        name = Store.display_name(doc)
        tags = doc.get("tags") or []
        had_file = bool(doc.get("file_id"))

        try:
            removed = Store.delete(storage_id, identity)
        except Exception as exc:  # noqa: BLE001
            logger.error("delete_doc: delete raised for %s: %s", storage_id, exc)
            failed.append(f"`{storage_id}` — {type(exc).__name__}: {exc}")
            continue

        if not removed or Store.get(storage_id, identity):
            logger.error("delete_doc: %s still present after delete", storage_id)
            failed.append(f"`{storage_id}` — the database reported no deletion")
            continue

        detail = f"| {_md(name)} | `{storage_id}` "
        detail += f"| {_md(', '.join(tags)) if tags else '—'} "
        detail += f"| {'file and text' if had_file else 'text only'} |"
        done.append(detail)
        logger.info("delete_doc: %s removed for identity %s", storage_id, identity or "-")

    lines: list[str] = []
    if done:
        lines += [
            f"**DELETED** — {len(done)} document{'s' if len(done) != 1 else ''} permanently "
            "removed from storage and from list_documents.",
            "",
            "| Document | Storage ID | Tags | Removed |",
            "| --- | --- | --- | --- |",
            *done,
        ]
    if missing:
        if lines:
            lines.append("")
        lines.append(
            "**Not found:** " + ", ".join(f"`{m}`" for m in missing) + " — no such document "
            "for this identity. It may already have been deleted, or the id may be wrong; "
            "check list_documents. Nothing was removed for these."
        )
    if failed:
        if lines:
            lines.append("")
        lines += ["**FAILED — still stored:**", "", *[f"- {f}" for f in failed]]

    if not done and not failed:
        # Nothing existed, so nothing happened — say so plainly rather than
        # letting a "DELETED" heading imply otherwise.
        return "[ERROR] NOT DELETED — " + "\n".join(lines).replace("**Not found:** ", "")

    lines += [
        "",
        "Deletion is permanent — the record and the original file are gone and cannot be "
        "recovered. Tell the user exactly which documents were removed."
        if done else
        "Nothing was deleted. Do not tell the user the document is gone.",
    ]
    return "\n".join(lines)


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

