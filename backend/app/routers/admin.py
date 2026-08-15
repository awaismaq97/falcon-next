"""
admin.py — Admin panel API router.

Login works for BOTH admin accounts and portal users.
Admin-only routes (user management, audit) are protected by require_admin.
The /admin/me and /admin/login routes work for everyone.

Routes (public):
  POST   /admin/login                  — authenticate any user, receive JWT

Routes (any authenticated user):
  GET    /admin/me                     — who am I + my identity_id + features

Routes (admin only):
  GET    /admin/users                  — list portal users
  POST   /admin/users                  — create portal user
  GET    /admin/users/{id}             — get one portal user
  PATCH  /admin/users/{id}             — update user
  DELETE /admin/users/{id}             — hard-delete portal user
  PUT    /admin/users/{id}/permissions — replace feature flags
  GET    /admin/audit                  — list admin audit trail
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from pydantic import BaseModel

import falcon.admin_audit as AdminAudit
import falcon.admin_users as AdminUsers
from falcon.admin_auth import (
    create_access_token,
    decode_access_token,
    verify_password,
)

router = APIRouter(tags=["admin"])
_bearer = HTTPBearer()

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    identity_id: str
    features: dict[str, bool] | None = None   # only set for portal users


class PortalUserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    features: dict[str, bool] | None = None


class PortalUserUpdateRequest(BaseModel):
    display_name: str | None = None
    password: str | None = None
    disabled: bool | None = None


class PermissionsUpdateRequest(BaseModel):
    features: dict[str, bool]


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

def _decode(creds: HTTPAuthorizationCredentials) -> dict[str, Any]:
    """Decode JWT or raise 401."""
    try:
        return decode_access_token(creds.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_any_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict[str, Any]:
    """Accept any valid JWT (admin or portal user)."""
    return _decode(creds)


def require_admin(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict[str, Any]:
    """Accept only admin-role JWTs."""
    payload = _decode(creds)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return payload


# ---------------------------------------------------------------------------
# Login — checks admin_users first, then portal_users
# ---------------------------------------------------------------------------

@router.post("/admin/login", response_model=LoginResponse)
def admin_login(req: LoginRequest) -> LoginResponse:
    """Authenticate an admin or portal user and return a JWT."""

    # 1. Try admin_users first
    user = AdminUsers.get_admin_by_username(req.username)
    if user and verify_password(req.password, user.get("password_hash", "")):
        identity_id = user.get("identity_id", "default")
        role = user.get("role", "admin")
        token = create_access_token(
            user_id=str(user["_id"]),
            username=req.username,
            role=role,
            identity_id=identity_id,
        )
        AdminAudit.log_admin_action(
            actor=req.username,
            action="login_success",
            details={"role": role, "identity_id": identity_id},
        )
        return LoginResponse(
            access_token=token,
            username=req.username,
            role=role,
            identity_id=identity_id,
            features=None,  # admins have full access — no feature restriction
        )

    # 2. Try portal_users
    portal = AdminUsers.get_portal_user_by_username(req.username)
    if portal and verify_password(req.password, portal.get("password_hash", "")):
        if portal.get("disabled"):
            AdminAudit.log_admin_action(
                actor=req.username,
                action="login_failed",
                details={"reason": "account disabled"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is disabled. Contact your administrator.",
            )
        identity_id = portal.get("identity_id", req.username)
        role = "user"
        token = create_access_token(
            user_id=str(portal["_id"]),
            username=req.username,
            role=role,
            identity_id=identity_id,
        )
        AdminAudit.log_admin_action(
            actor=req.username,
            action="login_success",
            details={"role": role, "identity_id": identity_id},
        )
        return LoginResponse(
            access_token=token,
            username=req.username,
            role=role,
            identity_id=identity_id,
            features=portal.get("features"),
        )

    # 3. Neither matched
    AdminAudit.log_admin_action(
        actor=req.username,
        action="login_failed",
        details={"reason": "bad credentials"},
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password.",
    )


# ---------------------------------------------------------------------------
# /admin/me — works for any authenticated user
# ---------------------------------------------------------------------------

@router.get("/admin/me")
def admin_me(payload: dict = Depends(require_any_user)) -> dict:
    """Return the current user's identity info from the JWT."""
    return {
        "username": payload["username"],
        "role": payload["role"],
        "identity_id": payload.get("identity_id", "default"),
    }


# ---------------------------------------------------------------------------
# Admin-only: user management
# ---------------------------------------------------------------------------

@router.get("/admin/users")
def list_users(payload: dict = Depends(require_admin)) -> dict:
    return {"users": AdminUsers.list_portal_users()}


@router.post("/admin/users", status_code=201)
def create_user(
    req: PortalUserCreateRequest,
    payload: dict = Depends(require_admin),
) -> dict:
    try:
        user_id = AdminUsers.create_portal_user(
            username=req.username,
            password=req.password,
            features=req.features,
            display_name=req.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    AdminAudit.log_admin_action(
        actor=payload["username"],
        action="create_user",
        target=req.username,
        details={"user_id": user_id, "features": req.features},
    )
    user = AdminUsers.get_portal_user_by_id(user_id)
    return user or {"id": user_id}


@router.get("/admin/users/{user_id}")
def get_user(user_id: str, payload: dict = Depends(require_admin)) -> dict:
    user = AdminUsers.get_portal_user_by_id(user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    return user


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: str,
    req: PortalUserUpdateRequest,
    payload: dict = Depends(require_admin),
) -> dict:
    patch: dict = {}
    if req.display_name is not None:
        patch["display_name"] = req.display_name
    if req.password is not None:
        patch["password"] = req.password
    if req.disabled is not None:
        patch["disabled"] = req.disabled
    if not patch:
        raise HTTPException(400, "No fields to update.")
    ok = AdminUsers.update_portal_user(user_id, patch)
    if not ok:
        raise HTTPException(404, "User not found.")
    AdminAudit.log_admin_action(
        actor=payload["username"],
        action="update_user",
        target=user_id,
        details={k: v for k, v in patch.items() if k != "password"},
    )
    user = AdminUsers.get_portal_user_by_id(user_id)
    return user or {"id": user_id}


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: str, payload: dict = Depends(require_admin)) -> dict:
    user = AdminUsers.get_portal_user_by_id(user_id)
    target_name = user.get("username", user_id) if user else user_id
    ok = AdminUsers.delete_portal_user(user_id)
    if not ok:
        raise HTTPException(404, "User not found.")
    AdminAudit.log_admin_action(
        actor=payload["username"],
        action="delete_user",
        target=target_name,
    )
    return {"deleted": user_id}


@router.put("/admin/users/{user_id}/permissions")
def set_permissions(
    user_id: str,
    req: PermissionsUpdateRequest,
    payload: dict = Depends(require_admin),
) -> dict:
    ok = AdminUsers.set_user_features(user_id, req.features)
    if not ok:
        raise HTTPException(404, "User not found.")
    AdminAudit.log_admin_action(
        actor=payload["username"],
        action="update_permissions",
        target=user_id,
        details={"features": req.features},
    )
    user = AdminUsers.get_portal_user_by_id(user_id)
    return user or {"id": user_id}


# ---------------------------------------------------------------------------
# Admin-only: audit log
# ---------------------------------------------------------------------------

@router.get("/admin/audit")
def get_admin_audit(
    limit: int = 200,
    payload: dict = Depends(require_admin),
) -> dict:
    return {"records": AdminAudit.list_admin_audit(limit=limit)}
