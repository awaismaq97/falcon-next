"""
google_drive.py — Google Drive access for the watcher, connected once and kept.

What this is for
----------------
Three watcher tools sit on top of this module: ``drive_list`` (what documents are
in the folder), ``drive_summarize`` (read one and hand back compact bullets) and
``drive_upload`` (put a stored document into the folder). None of them own any
Google plumbing — it all lives here, so there is one place that holds a
credential, one place that talks to Drive, and one place that decides what the
folder boundary means.

Staying connected
-----------------
The requirement is that connecting happens once, not every few days. Four things
together make that true, and all four are necessary:

* ``access_type=offline`` with ``prompt=consent`` on the authorisation URL, which
  is the only way Google issues a refresh token at all. Without ``prompt=consent``
  a second authorisation for an already-approved client returns an access token
  and no refresh token, and the connection silently becomes an hour long.
* The OAuth client being **published** rather than left in Testing. A client in
  Testing issues refresh tokens that expire after seven days, which is exactly
  the "put the token in again after some time" failure. Publishing is done in the
  Cloud Console, not here, so :func:`status` reports it as a thing to check when
  a refresh starts failing.
* The refresh token stored in MongoDB rather than in a file or in memory.
  DigitalOcean rebuilds the container image from git on every deploy, so anything
  written to disk at runtime is gone on the next push — the same failure that
  used to reset the watcher persona. Mongo survives redeploys and is shared by
  every instance, so scaling past one container does not require re-authorising.
* Access tokens minted on demand from that refresh token and cached in process
  memory for their hour. Nothing persists an access token; there is no point,
  and a stored one would only ever be stale.

Google does not rotate refresh tokens for web clients, so the stored value stays
valid indefinitely. It can still be revoked by the account owner, and it does
expire after six months of complete disuse — which is why the Lumen Guard check
refreshes through it rather than merely reading a flag. A refresh is itself use,
so a monitored deployment can never reach the inactivity cutoff.

Scope and blast radius
----------------------
Google has no folder-scoped OAuth scope, so "only this folder" cannot be
expressed as a permission and is enforced here instead: every listing and every
read is constrained to ``GOOGLE_DRIVE_FOLDER_ID`` (or a folder proven to be
inside it), and every upload is created with that folder as its parent.

The default scope pair is chosen so the damage a bug could do is bounded by the
grant and not only by this file:

    drive.readonly   read, and nothing else, across the account
    drive.file       full access to files this app itself created — and no others

That combination cannot modify, overwrite or delete anything that already existed
in the account, because the only writable scope covers app-created files. It is
the tightest pairing that still satisfies "list documents already in the folder"
and "upload new ones". Narrowing to ``drive.file`` alone is supported via
``GOOGLE_DRIVE_SCOPES`` and is genuinely tighter, at the cost of the agents
seeing only files they uploaded themselves.

Nothing here ever calls the permissions API. There is no code path that can make
a file public, and adding one would be the only way to get a shareable link.

Configuration
-------------
    GOOGLE_OAUTH_CLIENT_JSON          Contents of the downloaded client secrets
                                      JSON. Preferred in production — it is an
                                      environment variable, so no file has to
                                      survive a rebuild.
    GOOGLE_OAUTH_CLIENT_SECRETS_FILE  Path to that same JSON, for local dev.
    GOOGLE_OAUTH_REDIRECT_URI         Must match a redirect URI registered on the
                                      OAuth client, exactly.
    GOOGLE_DRIVE_FOLDER_ID            The one folder everything is scoped to.
    GOOGLE_DRIVE_SCOPES               Optional override (space separated).
    DRIVE_SUMMARY_MODEL               Optional. Default openai/gpt-4o-mini.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from falcon.db import get_db

logger = logging.getLogger("falcon.google_drive")

# Singleton document holding the connection. One Drive account serves the whole
# deployment: the folder is a shared workspace, not per-identity storage, and a
# per-identity connection would mean every user completing their own OAuth flow
# to reach the same folder.
AUTH_COLL = "google_drive_auth"
_AUTH_ID = "singleton"

# Short-lived CSRF state for an authorisation in flight. TTL-indexed in db.py,
# so an abandoned flow cleans itself up.
STATE_COLL = "google_oauth_state"
STATE_TTL_SECONDS = 600

# HKDF label for the refresh token's encryption key. Distinct from the username
# label, so the two ciphertexts are not interchangeable.
_TOKEN_PURPOSE = b"falcon/google-drive-refresh-token/v1"

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
_API = "https://www.googleapis.com/drive/v3"
_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/drive.file"
)

_HTTP_TIMEOUT = 30
# Matches file_store.MAX_FILE_BYTES: a document larger than this could not be
# stored locally either, so refusing early saves the download.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

# Refresh this long before the access token actually expires, so a request that
# starts just under the wire does not arrive just over it.
_EXPIRY_SKEW_SECONDS = 120

# How often the liveness probe does a real Drive call rather than relying on the
# cached token. Frequent enough to notice a revoked grant the same day, rare
# enough to be invisible in quota.
_PROBE_INTERVAL_SECONDS = 6 * 3600


class DriveError(Exception):
    """A Drive operation failed in a way the caller should report, not retry."""


class NotConnected(DriveError):
    """No usable Google credential is stored. The fix is to connect, not retry."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def folder_id() -> str:
    return os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()


def scopes() -> str:
    return os.environ.get("GOOGLE_DRIVE_SCOPES", "").strip() or DEFAULT_SCOPES


def redirect_uri() -> str:
    return os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()


def summary_model() -> str:
    return os.environ.get("DRIVE_SUMMARY_MODEL", "").strip() or "openai/gpt-4o-mini"


def _client_config() -> dict:
    """The OAuth client id and secret, from the environment or the secrets file.

    Accepts the downloaded JSON exactly as Google produces it, under either the
    ``web`` or ``installed`` key, so nobody has to unwrap it by hand and get the
    nesting wrong.
    """
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "").strip()
    source = "GOOGLE_OAUTH_CLIENT_JSON"

    if not raw:
        path = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "").strip()
        if not path:
            raise NotConnected(
                "Google Drive is not configured. Set GOOGLE_OAUTH_CLIENT_JSON to the "
                "contents of the OAuth client secrets file downloaded from the Cloud "
                "Console (or GOOGLE_OAUTH_CLIENT_SECRETS_FILE to its path)."
            )
        if not os.path.exists(path):
            raise NotConnected(f"GOOGLE_OAUTH_CLIENT_SECRETS_FILE points at {path!r}, which does not exist.")
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        source = path

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise NotConnected(f"The OAuth client secrets in {source} are not valid JSON: {exc}") from exc

    cfg = data.get("web") or data.get("installed") or data
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = (cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise NotConnected(
            f"The OAuth client secrets in {source} have no client_id/client_secret. "
            "Download the JSON again from the Cloud Console credentials page."
        )
    return {"client_id": client_id, "client_secret": client_secret}


def configured() -> bool:
    """Whether the deployment has everything needed to attempt a connection."""
    try:
        _client_config()
    except NotConnected:
        return False
    return bool(folder_id() and redirect_uri())


def config_problems() -> list[str]:
    """Every missing piece of configuration, so one visit to the settings fixes all."""
    problems: list[str] = []
    try:
        _client_config()
    except NotConnected as exc:
        problems.append(str(exc))
    if not folder_id():
        problems.append(
            "GOOGLE_DRIVE_FOLDER_ID is not set — open the folder in Drive and copy the "
            "id out of the URL (drive.google.com/drive/folders/<id>)."
        )
    if not redirect_uri():
        problems.append(
            "GOOGLE_OAUTH_REDIRECT_URI is not set. It must match a redirect URI "
            "registered on the OAuth client exactly, e.g. "
            "https://<your-host>/api/drive/callback"
        )
    return problems


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

def _coll():
    return get_db()[AUTH_COLL]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record() -> dict | None:
    try:
        return _coll().find_one({"_id": _AUTH_ID})
    except Exception as exc:  # noqa: BLE001
        logger.error("google_drive: could not read the stored connection: %s", exc)
        return None


def connected() -> bool:
    doc = _record()
    return bool(doc and doc.get("refresh_token_enc"))


def _store_refresh_token(
    token: str, *, account_email: str, connected_by: str, granted_scopes: str
) -> None:
    from falcon.admin_auth import encrypt_secret

    _coll().update_one(
        {"_id": _AUTH_ID},
        {
            "$set": {
                "refresh_token_enc": encrypt_secret(token, purpose=_TOKEN_PURPOSE),
                "account_email": account_email,
                "granted_scopes": granted_scopes,
                "connected_at": _now(),
                "connected_by": connected_by,
                "client_id": _client_config()["client_id"],
                "folder_id": folder_id(),
                # A fresh grant clears whatever the previous one died of.
                "last_error": "",
                "last_error_at": None,
            }
        },
        upsert=True,
    )
    logger.info(
        "google_drive: connected as %s by %r (scopes: %s)",
        account_email or "unknown account", connected_by, granted_scopes,
    )


def _read_refresh_token() -> str:
    from falcon.admin_auth import decrypt_secret

    doc = _record()
    if not doc or not doc.get("refresh_token_enc"):
        raise NotConnected(
            "Google Drive is not connected. An admin needs to connect it once from "
            "the admin panel; after that it stays connected."
        )
    try:
        return decrypt_secret(doc["refresh_token_enc"], purpose=_TOKEN_PURPOSE)
    except ValueError as exc:
        raise NotConnected(f"{exc} Reconnect Google Drive from the admin panel.") from exc


def _note_error(message: str) -> None:
    """Record why the credential stopped working, for status and Lumen Guard."""
    try:
        _coll().update_one(
            {"_id": _AUTH_ID},
            {"$set": {"last_error": message[:500], "last_error_at": _now()}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("google_drive: could not record the error: %s", exc)


def disconnect(actor: str = "") -> bool:
    """Forget the stored credential, and tell Google to revoke it.

    Revocation is best effort and deliberately not allowed to fail the
    disconnect: if Google cannot be reached, the right outcome is still that
    this deployment no longer holds a usable token.
    """
    doc = _record()
    if not doc:
        return False

    try:
        token = _read_refresh_token()
    except NotConnected:
        token = ""

    if token:
        try:
            requests.post(
                _REVOKE_ENDPOINT, data={"token": token}, timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("google_drive: revoke call failed (disconnecting anyway): %s", exc)

    _coll().delete_one({"_id": _AUTH_ID})
    with _token_lock:
        _cached_token["value"] = ""
        _cached_token["expires_at"] = 0.0
    # Reconnecting may point at a different folder or a different account, so
    # nothing proven under the old grant should carry over.
    _verified_folders.clear()
    logger.info("google_drive: disconnected by %r", actor or "unknown")
    return True


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def begin_auth(actor: str) -> str:
    """The Google consent URL to send an admin to. Records a one-use state token.

    The state is stored server-side rather than signed and handed out, because
    the callback has to be reachable without a bearer token — a browser redirect
    from Google cannot carry an Authorization header — and an unguessable value
    that this server issued is what stands in for the missing one.
    """
    problems = config_problems()
    if problems:
        raise NotConnected(" ".join(problems))

    state = secrets.token_urlsafe(32)
    get_db()[STATE_COLL].insert_one({
        "state": state,
        "actor": actor,
        "created_at": _now(),
    })

    from urllib.parse import urlencode

    params = {
        "client_id": _client_config()["client_id"],
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": scopes(),
        # The pair that produces a refresh token rather than an hour of access.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_ENDPOINT}?{urlencode(params)}"


def _consume_state(state: str) -> str:
    """Validate and burn a state token, returning who started the flow."""
    doc = get_db()[STATE_COLL].find_one_and_delete({"state": (state or "").strip()})
    if not doc:
        raise DriveError(
            "This authorisation link is not one this server issued, or it has expired. "
            "Start the connection again from the admin panel."
        )
    created = doc.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if _now() - created > timedelta(seconds=STATE_TTL_SECONDS):
            raise DriveError("This authorisation link has expired. Start again from the admin panel.")
    return doc.get("actor", "")


def complete_auth(code: str, state: str) -> dict:
    """Exchange the authorisation code for tokens and store the refresh token."""
    actor = _consume_state(state)
    cfg = _client_config()

    try:
        resp = requests.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DriveError(f"Could not reach Google to exchange the authorisation code: {exc}") from exc

    if resp.status_code != 200:
        raise DriveError(f"Google rejected the authorisation code: {_describe(resp)}")

    body = resp.json()
    refresh_token = (body.get("refresh_token") or "").strip()
    access_token = (body.get("access_token") or "").strip()

    if not refresh_token:
        # Without one, the connection lasts an hour — which is the exact failure
        # this module exists to prevent, so it is refused rather than stored.
        raise DriveError(
            "Google returned an access token but no refresh token, so the connection "
            "would stop working within the hour. This happens when the account has "
            "already approved this client and consent was not forced. Revoke Falcon's "
            "access at myaccount.google.com/permissions and connect again."
        )

    email = _account_email(access_token)
    _store_refresh_token(
        refresh_token,
        account_email=email,
        connected_by=actor,
        granted_scopes=body.get("scope", "") or scopes(),
    )

    # Seed the cache so the first tool call after connecting does not immediately
    # spend a refresh.
    with _token_lock:
        _cached_token["value"] = access_token
        _cached_token["expires_at"] = time.monotonic() + int(body.get("expires_in", 3600))

    return {"account_email": email, "connected_by": actor, "scopes": body.get("scope", "")}


def _account_email(access_token: str) -> str:
    """Which Google account just authorised. Best effort — used for display only."""
    try:
        resp = requests.get(
            f"{_API}/about",
            params={"fields": "user(emailAddress)"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return ((resp.json().get("user") or {}).get("emailAddress") or "").strip()
    except requests.RequestException:
        pass
    return ""


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

# Cached per process. Deliberately not persisted: an access token is valid for an
# hour, and a stored one would be stale far more often than it was useful.
_cached_token: dict[str, Any] = {"value": "", "expires_at": 0.0}
_token_lock = threading.Lock()


def access_token(force_refresh: bool = False) -> str:
    """A valid access token, refreshing through the stored refresh token as needed.

    Called on every Drive operation. The lock makes a burst of concurrent tool
    calls perform one refresh between them rather than one each.
    """
    with _token_lock:
        if (
            not force_refresh
            and _cached_token["value"]
            and time.monotonic() < _cached_token["expires_at"] - _EXPIRY_SKEW_SECONDS
        ):
            return _cached_token["value"]

        refresh_token = _read_refresh_token()
        cfg = _client_config()

        try:
            resp = requests.post(
                _TOKEN_ENDPOINT,
                data={
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise DriveError(f"Could not reach Google to refresh the access token: {exc}") from exc

        if resp.status_code != 200:
            detail = _describe(resp)
            # invalid_grant is terminal: the refresh token is revoked, expired
            # through six months of disuse, or was issued by a Testing-mode
            # client and has passed its seven days. Retrying cannot fix any of
            # those, so say what actually has to happen.
            if "invalid_grant" in detail:
                message = (
                    "Google has revoked the stored Drive credential. Reconnect from the "
                    "admin panel. If this keeps happening every few days, the OAuth "
                    "client is still in Testing mode — publish it in the Cloud Console "
                    "so its refresh tokens stop expiring."
                )
                _note_error(message)
                raise NotConnected(message)
            _note_error(detail)
            raise DriveError(f"Google refused to refresh the access token: {detail}")

        body = resp.json()
        token = (body.get("access_token") or "").strip()
        if not token:
            raise DriveError("Google's refresh response contained no access token.")

        _cached_token["value"] = token
        _cached_token["expires_at"] = time.monotonic() + int(body.get("expires_in", 3600))

        # Google does not rotate refresh tokens for web clients, but storing one
        # when it does appear costs nothing and removes a whole class of future
        # breakage.
        rotated = (body.get("refresh_token") or "").strip()
        if rotated and rotated != refresh_token:
            from falcon.admin_auth import encrypt_secret

            _coll().update_one(
                {"_id": _AUTH_ID},
                {"$set": {"refresh_token_enc": encrypt_secret(rotated, purpose=_TOKEN_PURPOSE)}},
            )
            logger.info("google_drive: stored a rotated refresh token")

    try:
        _coll().update_one(
            {"_id": _AUTH_ID},
            {"$set": {"last_refresh_at": _now(), "last_error": "", "last_error_at": None}},
        )
    except Exception:  # noqa: BLE001 — bookkeeping, never worth failing a call
        pass

    return token


def _describe(resp: requests.Response) -> str:
    """A readable one-liner from a Google error response."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code} — {resp.text[:300]}"
    err = body.get("error")
    if isinstance(err, dict):
        detail = err.get("message") or json.dumps(err)[:300]
    else:
        detail = f"{err or ''} {body.get('error_description', '')}".strip() or str(body)[:300]
    return f"HTTP {resp.status_code} — {detail}"


# ---------------------------------------------------------------------------
# Drive REST
# ---------------------------------------------------------------------------

def _request(method: str, url: str, **kwargs) -> requests.Response:
    """One authenticated Drive call, retried once on a 401.

    The retry covers the narrow case of a token that expired between the skew
    check and the request landing. Anything else is returned as-is for the
    caller to describe.
    """
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {access_token()}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise DriveError(f"Could not reach Google Drive: {exc}") from exc

    if resp.status_code == 401:
        headers["Authorization"] = f"Bearer {access_token(force_refresh=True)}"
        try:
            resp = requests.request(method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise DriveError(f"Could not reach Google Drive: {exc}") from exc

    return resp


# Native Google formats, and what to export each one as. Sheets have no
# text/plain export, so they come back as CSV, which is what a spreadsheet's text
# actually looks like anyway.
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "txt"),
    "application/vnd.google-apps.presentation": ("text/plain", "txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "csv"),
}

# Binary formats worth downloading, and the extension falcon.text_extract needs
# in order to pick a parser.
BINARY_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/rtf": "rtf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/json": "json",
    "text/html": "html",
    "application/xml": "xml",
    "text/xml": "xml",
}

FOLDER_TYPE = "application/vnd.google-apps.folder"

# Everything the agents will show and read. Anything else in the folder — images,
# video, archives — is simply not listed, because none of the three tools can do
# anything with it.
READABLE_TYPES = frozenset(GOOGLE_EXPORTS) | frozenset(BINARY_TYPES)

_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, size, owners(emailAddress))"


def _shared_drive_params() -> dict:
    """Flags that make every call work for a folder on a shared drive too."""
    return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


# Subfolders already proven to sit inside the configured root, with the time each
# was proven. Walking the parent chain costs one metadata call per level, and a
# single listing resolves the same folder more than once — without this, opening
# a subfolder pays for the walk twice over.
#
# Expiring rather than permanent, because the answer can change underneath us: a
# folder moved out of the root in the Drive UI must stop being reachable, and a
# cache with no TTL would keep it reachable until the process restarted.
_FOLDER_CACHE_TTL = 600.0
_verified_folders: dict[str, float] = {}


def resolve_folder(target: str = "") -> str:
    """The folder a call should act on, refusing anything outside the configured root.

    Passing nothing means the configured folder. Passing a folder id is allowed
    only when that folder is a descendant of the configured one, so "list this
    subfolder" works while "list some other folder in my Drive" does not.

    This is where "only this folder" is actually enforced. Google has no
    folder-scoped OAuth scope, so the grant itself cannot express the boundary —
    ``drive.readonly`` can read the whole account. Every listing and every read
    goes through here, which is what makes the boundary real.
    """
    root = folder_id()
    if not root:
        raise NotConnected(
            "GOOGLE_DRIVE_FOLDER_ID is not set, so there is no folder to act on."
        )
    target = (target or "").strip()
    if not target or target == root:
        return root

    proven_at = _verified_folders.get(target)
    if proven_at is not None and time.monotonic() - proven_at < _FOLDER_CACHE_TTL:
        return target

    seen: set[str] = set()
    current = target
    # Walk up the parent chain. Bounded because Drive's hierarchy is finite and
    # `seen` stops a cycle that a corrupt response could otherwise turn into a
    # hang.
    for _ in range(20):
        if current in seen:
            break
        seen.add(current)
        meta = get_metadata(current, fields="id, name, mimeType, parents")
        if meta.get("mimeType") != FOLDER_TYPE and current == target:
            raise DriveError(f"{target!r} is a file, not a folder.")
        parents = meta.get("parents") or []
        if root in parents:
            _verified_folders[target] = time.monotonic()
            return target
        if not parents:
            break
        current = parents[0]

    raise DriveError(
        f"Folder {target!r} is not inside the configured Drive folder. These tools only "
        "reach that folder and the folders within it."
    )


def get_metadata(file_id: str, fields: str = "id, name, mimeType, modifiedTime, size, parents") -> dict:
    """Metadata for one file or folder."""
    resp = _request(
        "GET",
        f"{_API}/files/{file_id}",
        params={"fields": fields, **_shared_drive_params()},
    )
    if resp.status_code == 404:
        raise DriveError(
            f"No file with id {file_id!r} is visible to this connection. Check the id "
            "with drive_list."
        )
    if resp.status_code != 200:
        raise DriveError(f"Drive refused to describe {file_id!r}: {_describe(resp)}")
    return resp.json()


def _escape(value: str) -> str:
    """Escape a literal for a Drive query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def list_documents(
    folder: str = "", term: str = "", limit: int = 100, include_folders: bool = True
) -> list[dict]:
    """Readable documents in the folder, newest change first.

    ``term`` matches names, using Drive's own ``contains`` rather than a local
    filter so a large folder does not have to be paged through in full.
    """
    target = resolve_folder(folder)

    clauses = [f"'{_escape(target)}' in parents", "trashed = false"]
    if term.strip():
        clauses.append(f"name contains '{_escape(term.strip())}'")
    query = " and ".join(clauses)

    files: list[dict] = []
    page_token = ""
    # Bounded paging: the agents show a table, and a folder needing more than
    # this many pages needs a search term rather than a longer table.
    for _ in range(10):
        params = {
            "q": query,
            "fields": _LIST_FIELDS,
            "orderBy": "modifiedTime desc",
            "pageSize": str(min(100, max(1, limit))),
            **_shared_drive_params(),
        }
        if page_token:
            params["pageToken"] = page_token

        resp = _request("GET", f"{_API}/files", params=params)
        if resp.status_code != 200:
            raise DriveError(f"Drive refused to list the folder: {_describe(resp)}")

        body = resp.json()
        for f in body.get("files") or []:
            mime = f.get("mimeType", "")
            if mime == FOLDER_TYPE:
                if include_folders:
                    files.append(f)
            elif mime in READABLE_TYPES:
                files.append(f)
        if len(files) >= limit:
            break
        page_token = body.get("nextPageToken") or ""
        if not page_token:
            break

    return files[:limit]


def download_text(file_id: str) -> dict:
    """Plain text of one Drive document, with the metadata needed to name it.

    Returns ``{name, mime_type, text, chars, modified, truncated}``. Google-native
    formats are exported directly; everything else is downloaded and parsed by
    falcon.text_extract, which is the same code path an uploaded file takes.
    """
    from falcon.documents_store import MAX_TEXT_CHARS
    from falcon.text_extract import UnsupportedDocument, extract

    meta = get_metadata(file_id)
    name = meta.get("name") or file_id
    mime = meta.get("mimeType", "")

    if mime == FOLDER_TYPE:
        raise DriveError(f"{name!r} is a folder, not a document. Use drive_list on it instead.")

    if mime in GOOGLE_EXPORTS:
        export_mime, ext = GOOGLE_EXPORTS[mime]
        resp = _request(
            "GET",
            f"{_API}/files/{file_id}/export",
            params={"mimeType": export_mime, **_shared_drive_params()},
        )
        if resp.status_code == 403 and "exportSizeLimitExceeded" in resp.text:
            raise DriveError(
                f"{name!r} is too large for Google to export as text (the limit is 10 MB). "
                "Ask for a smaller section, or download it as a PDF and upload that."
            )
        if resp.status_code != 200:
            raise DriveError(f"Drive refused to export {name!r}: {_describe(resp)}")
        text = resp.content.decode("utf-8", errors="replace")

    elif mime in BINARY_TYPES:
        size = int(meta.get("size") or 0)
        if size > MAX_DOWNLOAD_BYTES:
            raise DriveError(
                f"{name!r} is {size / 1048576:.1f} MB, over the "
                f"{MAX_DOWNLOAD_BYTES / 1048576:.0f} MB limit for reading a document."
            )
        resp = _request(
            "GET",
            f"{_API}/files/{file_id}",
            params={"alt": "media", **_shared_drive_params()},
        )
        if resp.status_code != 200:
            raise DriveError(f"Drive refused to download {name!r}: {_describe(resp)}")
        try:
            text = extract(name, resp.content, BINARY_TYPES[mime])
        except UnsupportedDocument as exc:
            raise DriveError(f"Could not read {name!r}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise DriveError(f"Could not read {name!r}: {type(exc).__name__}: {exc}") from exc

    else:
        raise DriveError(
            f"{name!r} is a {mime or 'unknown'} file, which has no text to read. These "
            "tools handle Google Docs, Sheets, Slides, PDF, Word, Excel, PowerPoint and "
            "plain-text formats."
        )

    text = (text or "").strip()
    if not text:
        raise DriveError(
            f"{name!r} has no extractable text. A scanned PDF with no text layer reads "
            "as empty — it would need OCR, which is not set up here."
        )

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    return {
        "id": file_id,
        "name": name,
        "mime_type": mime,
        "text": text,
        "chars": len(text),
        "modified": meta.get("modifiedTime", ""),
        "truncated": truncated,
    }


def upload(data: bytes, name: str, content_type: str = "", folder: str = "") -> dict:
    """Upload bytes into the folder as a new file. Never overwrites, never shares.

    A resumable upload rather than a multipart one: Drive caps multipart uploads
    at 5 MB, and the documents this handles go to 25 MB. The extra round trip is
    what lets a large PDF work at all.

    No permission is ever set on the created file, so it inherits the folder's
    access and nothing else. There is deliberately no code here that could
    produce a shareable link.
    """
    target = resolve_folder(folder)
    name = (name or "untitled").strip()
    content_type = (content_type or "application/octet-stream").strip()

    if not data:
        raise DriveError("There is nothing to upload — the file is empty.")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise DriveError(
            f"{name!r} is {len(data) / 1048576:.1f} MB, over the "
            f"{MAX_DOWNLOAD_BYTES / 1048576:.0f} MB upload limit."
        )

    # 1. Announce the upload and get a session URL.
    start = _request(
        "POST",
        f"{_UPLOAD_API}/files",
        params={"uploadType": "resumable", **_shared_drive_params()},
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(len(data)),
        },
        data=json.dumps({"name": name, "parents": [target]}).encode("utf-8"),
    )
    if start.status_code not in (200, 201):
        raise DriveError(f"Drive refused to start the upload of {name!r}: {_describe(start)}")

    session_url = start.headers.get("Location", "")
    if not session_url:
        raise DriveError("Drive accepted the upload request but returned no session URL.")

    # 2. Send the whole body. The session URL carries its own authorisation, so
    #    this goes out without the bearer header.
    try:
        finish = requests.put(
            session_url,
            data=data,
            headers={"Content-Type": content_type, "Content-Length": str(len(data))},
            timeout=180,
        )
    except requests.RequestException as exc:
        raise DriveError(
            f"The upload of {name!r} was interrupted: {exc}. Check the Drive folder "
            "before trying again — it may or may not have completed."
        ) from exc

    if finish.status_code not in (200, 201):
        raise DriveError(f"Drive rejected the upload of {name!r}: {_describe(finish)}")

    created = finish.json()
    file_id = created.get("id", "")
    logger.info("google_drive: uploaded %r as %s (%d bytes)", name, file_id, len(data))

    meta = get_metadata(file_id, fields="id, name, mimeType, size, modifiedTime, webViewLink")
    return {
        "id": file_id,
        "name": meta.get("name") or name,
        "mime_type": meta.get("mimeType") or content_type,
        "bytes": int(meta.get("size") or len(data)),
        "modified": meta.get("modifiedTime", ""),
        # The canonical Drive URL. Not a public link: the file has no permissions
        # beyond the folder's, so this only opens for someone already allowed in.
        "link": meta.get("webViewLink", ""),
    }


# ---------------------------------------------------------------------------
# Status + liveness
# ---------------------------------------------------------------------------

def status() -> dict[str, Any]:
    """Everything the admin panel and Lumen Guard need, without a Drive call."""
    doc = _record() or {}
    return {
        "configured": configured(),
        "problems": config_problems(),
        "connected": bool(doc.get("refresh_token_enc")),
        "account_email": doc.get("account_email", ""),
        "connected_at": doc.get("connected_at"),
        "connected_by": doc.get("connected_by", ""),
        "granted_scopes": doc.get("granted_scopes", ""),
        "last_refresh_at": doc.get("last_refresh_at"),
        "last_error": doc.get("last_error", ""),
        "folder_id": folder_id(),
        "folder_name": doc.get("folder_name", ""),
        "redirect_uri": redirect_uri(),
        "scopes": scopes(),
        "summary_model": summary_model(),
        # A folder id stored at connection time that no longer matches the
        # environment means the deployment was pointed at a different folder
        # without reconnecting — worth surfacing, since the grant may not cover it.
        "folder_changed_since_connect": bool(
            doc.get("folder_id") and folder_id() and doc["folder_id"] != folder_id()
        ),
    }


def probe(force: bool = False) -> dict[str, Any]:
    """Prove the connection still works, and keep it from lapsing.

    Refreshing the access token is itself use of the refresh token, which is what
    stops the six-month inactivity expiry from ever being reached on a monitored
    deployment. The folder read is rate-limited to every few hours because its
    job is to catch a revoked grant or a moved folder, neither of which needs
    checking every minute.
    """
    doc = _record() or {}
    token = access_token()  # refreshes when due; raises when the grant is gone

    last = doc.get("last_probe_at")
    if isinstance(last, datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    due = (
        force
        or not isinstance(last, datetime)
        or (_now() - last) > timedelta(seconds=_PROBE_INTERVAL_SECONDS)
    )

    if not due:
        return {
            "ok": True,
            "token": bool(token),
            "folder_checked": False,
            "folder_name": doc.get("folder_name", ""),
        }

    meta = get_metadata(folder_id(), fields="id, name, mimeType")
    if meta.get("mimeType") != FOLDER_TYPE:
        raise DriveError(
            f"GOOGLE_DRIVE_FOLDER_ID points at {meta.get('name') or folder_id()!r}, "
            "which is not a folder."
        )

    _coll().update_one(
        {"_id": _AUTH_ID},
        {"$set": {"last_probe_at": _now(), "folder_name": meta.get("name", "")}},
    )
    return {
        "ok": True,
        "token": True,
        "folder_checked": True,
        "folder_name": meta.get("name", ""),
    }
