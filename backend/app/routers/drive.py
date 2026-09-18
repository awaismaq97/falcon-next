"""
drive.py router — connect the deployment's Google Drive account, once.

Routes (admin only):
  GET    /drive/status       — configuration, connection state, last refresh
  POST   /drive/connect      — returns the Google consent URL to open
  POST   /drive/disconnect   — forget the credential and revoke it at Google
  POST   /drive/check        — prove the connection still works, right now

Mounted bare (no bearer token):
  GET    /drive/callback     — where Google sends the browser back

The callback is the one route here that cannot require authentication, for the
same reason the watcher's SSE stream cannot: it is reached by a browser redirect
that Google controls, and a redirect carries no Authorization header. It is
mounted on its own router so that exemption is visible at the mount point rather
than hidden inside a handler.

What stands in for the missing token is the ``state`` parameter. ``/drive/connect``
mints an unguessable one, stores it server-side against the admin who asked, and
the callback consumes it exactly once — so a callback can only complete a flow
that an authenticated admin started here, within ten minutes, and never twice.
An attacker who cannot read that value cannot reach the exchange at all, and one
who replays a used link gets the same refusal as an expired one.

The authorisation code itself is also single-use at Google's end, so even a
leaked callback URL cannot be replayed into a second credential.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

import falcon.admin_audit as AdminAudit
import falcon.google_drive as Drive
from app.deps import require_admin

logger = logging.getLogger("falcon.drive")

router = APIRouter(tags=["drive"])

# No blanket dependency — see the module docstring.
callback_router = APIRouter(tags=["drive"])


@router.get("/drive/status")
def drive_status(_: dict = Depends(require_admin)) -> dict[str, Any]:
    """Configuration and connection state, without calling Google."""
    return Drive.status()


@router.post("/drive/connect")
def drive_connect(auth: dict = Depends(require_admin)) -> dict[str, Any]:
    """Start the authorisation flow. Returns the URL for the admin to open.

    The URL is returned rather than redirected to, because the caller is a
    fetch() from the admin panel and a 302 to accounts.google.com would be
    followed by the browser's CORS machinery, not by the person.
    """
    actor = auth.get("username") or "admin"
    try:
        url = Drive.begin_auth(actor)
    except Drive.NotConnected as exc:
        raise HTTPException(400, str(exc))
    except Drive.DriveError as exc:
        raise HTTPException(400, str(exc))

    AdminAudit.log_admin_action(actor=actor, action="drive_connect_started")
    return {
        "auth_url": url,
        "redirect_uri": Drive.redirect_uri(),
        "scopes": Drive.scopes(),
        "message": (
            "Open this URL, sign in as the account that owns the Drive folder, and "
            "approve. The link is single-use and expires in ten minutes."
        ),
    }


@router.post("/drive/disconnect")
def drive_disconnect(auth: dict = Depends(require_admin)) -> dict[str, Any]:
    """Forget the stored credential and revoke it at Google."""
    actor = auth.get("username") or "admin"
    removed = Drive.disconnect(actor)
    AdminAudit.log_admin_action(actor=actor, action="drive_disconnect", details={"had_credential": removed})
    if not removed:
        raise HTTPException(400, "Google Drive was not connected.")
    return {"disconnected": True, **Drive.status()}


@router.post("/drive/check")
def drive_check(auth: dict = Depends(require_admin)) -> dict[str, Any]:
    """Refresh the token and read the folder now, rather than waiting for Lumen.

    ``force`` on the probe so pressing the button actually tests something — the
    scheduled probe only touches Drive every few hours, which is right for a
    monitor and useless for a person asking "is it working?".
    """
    try:
        result = Drive.probe(force=True)
    except Drive.NotConnected as exc:
        raise HTTPException(400, str(exc))
    except Drive.DriveError as exc:
        raise HTTPException(502, str(exc))

    AdminAudit.log_admin_action(actor=auth.get("username") or "admin", action="drive_check")
    return {"ok": True, **result, **Drive.status()}


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------

def _page(title: str, body: str, ok: bool) -> HTMLResponse:
    """A plain confirmation page.

    Deliberately a page and not JSON: a person is looking at this in a browser
    tab they were redirected into, and a raw JSON body would leave them unsure
    whether it worked. Self-contained, no scripts, no external requests.
    """
    colour = "#16a34a" if ok else "#dc2626"
    return HTMLResponse(
        status_code=200 if ok else 400,
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title></head>"
            "<body style=\"font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;"
            "background:#0b0b0c;color:#e5e5e5;display:flex;min-height:100vh;margin:0;"
            "align-items:center;justify-content:center;padding:24px\">"
            "<div style='max-width:36rem'>"
            f"<h1 style=\"color:{colour};font-size:1.25rem;margin:0 0 .75rem\">{title}</h1>"
            f"<p style='line-height:1.6;margin:0;color:#a3a3a3'>{body}</p>"
            "</div></body></html>"
        ),
    )


@callback_router.get("/drive/callback")
def drive_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """Where Google returns the browser after the consent screen."""
    if error:
        # The usual one is access_denied: somebody pressed Cancel.
        logger.info("drive: authorisation returned error=%r", error)
        return _page(
            "Google Drive was not connected",
            f"Google returned <code>{error}</code>. Nothing has changed — you can start "
            "the connection again from the admin panel.",
            ok=False,
        )

    if not code or not state:
        return _page(
            "Google Drive was not connected",
            "That callback was missing its authorisation code. Start the connection "
            "again from the admin panel.",
            ok=False,
        )

    try:
        result = Drive.complete_auth(code, state)
    except (Drive.DriveError, Drive.NotConnected) as exc:
        logger.error("drive: authorisation failed: %s", exc)
        return _page("Google Drive was not connected", str(exc), ok=False)
    except Exception as exc:  # noqa: BLE001 — this page must never be a stack trace
        logger.exception("drive: authorisation failed unexpectedly")
        return _page(
            "Google Drive was not connected",
            f"Something went wrong completing the connection: {type(exc).__name__}: {exc}",
            ok=False,
        )

    try:
        AdminAudit.log_admin_action(
            actor=result.get("connected_by") or "admin",
            action="drive_connected",
            details={"account_email": result.get("account_email", "")},
        )
    except Exception:  # noqa: BLE001 — the connection succeeded; logging it is secondary
        pass

    account = result.get("account_email") or "the authorised account"
    return _page(
        "Google Drive connected",
        f"Falcon is connected to <strong>{account}</strong> and will stay connected — "
        "the credential is stored and refreshes itself, so this does not need doing "
        "again. You can close this tab.",
        ok=True,
    )
