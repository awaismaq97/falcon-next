"""
deps.py — the single authentication and authorisation layer for the HTTP API.

Every protected router is mounted with ``dependencies=[Depends(require_user)]``
in ``app.main.create_app``, so authentication is a property of the mount rather
than of each handler. That is deliberate. Falcon previously required a token on
three routers and not on the other twelve, which left fifty-five routes — the
whole conversation, memory, audit, document and inference surface — reachable by
anyone who could reach the port. It happened because auth was opt-in per route,
and opting in fifty-five times is a thing you can simply forget to do.

Two distinct questions live here, and conflating them is what produced the
original hole:

``require_user`` / ``require_admin``
    *Who is calling.* Authentication. Applied to the whole router.

``require_identity``
    *Whose data they may touch.* Authorisation. Applied per route, because only
    the route knows where the identity comes from — a path segment, a query
    string, or a request body.

The second matters as much as the first. ``identity_id`` arrives from the client
on almost every endpoint, and the frontend takes it from a Zustand store
persisted to ``localStorage`` — so without a server-side check, changing one
string in devtools is enough to read another account's conversations. The rule
is that a portal user is pinned to the identity in their token, and only an
admin may name a different one.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

import falcon.admin_users as AdminUsers
from falcon.admin_auth import decode_access_token

# auto_error=False so a missing header produces our own 401 with a readable
# message, rather than FastAPI's bare "Not authenticated".
_bearer = HTTPBearer(auto_error=False)


def require_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Any authenticated account. Returns the decoded JWT claims."""
    if not creds:
        raise HTTPException(401, "Authentication required.")
    try:
        return decode_access_token(creds.credentials)
    except JWTError:
        raise HTTPException(401, "Invalid or expired authentication token.")


def require_admin(auth: dict = Depends(require_user)) -> dict:
    """An account with the admin role."""
    if auth.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    return auth


def is_admin(auth: dict) -> bool:
    return auth.get("role") == "admin"


def own_identity(auth: dict) -> str:
    """The identity this token is bound to."""
    return (auth.get("identity_id") or "default").strip()


def authorize_identity(auth: dict, requested: str) -> str:
    """Resolve the identity a request may act on, or refuse.

    An admin may name any identity, and naming none means their own. Everyone
    else gets their own regardless of what they asked for — except that asking
    for someone else's is refused outright rather than quietly redirected, so a
    misconfigured client fails visibly instead of writing to the wrong account.
    """
    own = own_identity(auth)
    asked = (requested or "").strip()
    if is_admin(auth):
        return asked or own
    if asked and asked != own:
        raise HTTPException(403, "You can only access your own identity.")
    return own


def require_identity(
    identity_id: str,
    auth: dict = Depends(require_user),
) -> str:
    """The authorised identity for a route that names one.

    Declare it in place of the raw parameter::

        @router.get("/identities/{identity_id}/history")
        def load_history(identity_id: str = Depends(require_identity)):
            ...

    FastAPI binds this dependency's ``identity_id`` to the route's path
    parameter where one exists, and to a required query parameter otherwise —
    which is what makes it usable unchanged on both shapes.
    """
    return authorize_identity(auth, identity_id)


def require_optional_identity(
    identity_id: str = "",
    auth: dict = Depends(require_user),
) -> str:
    """As ``require_identity``, but the parameter may be omitted.

    Omitted means "my own", which is the right default for a portal user and
    for an admin alike. Use this only where an absent identity is meaningful;
    prefer ``require_identity`` so the caller has to say what it means.
    """
    return authorize_identity(auth, identity_id)


def require_feature(name: str):
    """Refuse accounts whose admin has not granted the named feature.

    Read from the database rather than the token: features are not a JWT claim,
    so a token issued before a feature was revoked would otherwise keep working
    until it expired. Hiding a tab in the frontend is presentation; this is what
    actually stops the request.
    """

    def _check(auth: dict = Depends(require_user)) -> dict:
        if is_admin(auth):
            return auth
        user = AdminUsers.get_portal_user_by_id(str(auth.get("sub") or ""))
        if not user or not (user.get("features") or {}).get(name, False):
            raise HTTPException(
                403,
                f"The '{name}' feature is not enabled for this account. "
                "Ask an administrator to turn it on.",
            )
        return auth

    return _check
