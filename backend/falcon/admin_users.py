"""
admin_users.py — CRUD for admin accounts and portal (end) users.

Collections:
  admin_users   — system admins who can log into the admin panel
  portal_users  — end users created by admins; each has feature flags

Both collections store usernames encrypted with AES-256-GCM and passwords
hashed with bcrypt. Plaintext credentials are never stored.

Public API:
  Admin accounts:
    seed_first_admin(username, password)
    get_admin_by_username(username) -> dict | None
    list_admins() -> list[dict]

  Portal users:
    create_portal_user(username, password, features) -> str (inserted _id)
    get_portal_user_by_username(username) -> dict | None
    get_portal_user_by_id(user_id) -> dict | None
    list_portal_users() -> list[dict]
    update_portal_user(user_id, patch) -> bool
    disable_portal_user(user_id) -> bool
    delete_portal_user(user_id) -> bool
    set_user_features(user_id, features) -> bool
"""
from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId

from falcon.admin_auth import decrypt_username, encrypt_username, hash_password
from falcon.db import get_db

# ---------------------------------------------------------------------------
# Default feature set — matches the existing router names
# ---------------------------------------------------------------------------
DEFAULT_FEATURES: dict[str, bool] = {
    "chat": True,
    "memory": True,
    "context": True,
    "categories": True,
    "audit": True,
    "logs": True,
    "testing": True,
    "dualrun": True,
    "polymarket": True,
    "kalshi": True,
    "voice": True,
    "watcher": False,   # opt-in — admin must explicitly enable per user
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(doc: dict) -> dict:
    """Convert MongoDB doc to API-safe dict (stringify _id, decrypt username)."""
    if doc is None:
        return {}
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = str(doc["_id"])
    # Decrypt the stored username for the API caller
    try:
        out["username"] = decrypt_username(doc["username_enc"])
    except Exception:
        out["username"] = "<encrypted>"
    out.pop("username_enc", None)
    out.pop("password_hash", None)  # never return the hash
    # Ensure watcher_enabled is always present so the frontend doesn't need a null check
    out.setdefault("watcher_enabled", False)
    return out


# ---------------------------------------------------------------------------
# Admin accounts
# ---------------------------------------------------------------------------

def seed_first_admin(username: str, password: str, identity_id: str = "default") -> None:
    """Create the first admin account if no admins exist yet. Idempotent.

    The admin is bound to the existing 'default' identity so all data already
    in that identity (conversation history, memory, audit, etc.) belongs to them.
    """
    db = get_db()
    if db["admin_users"].count_documents({}) > 0:
        return  # already seeded
    db["admin_users"].insert_one(
        {
            "username_enc": encrypt_username(username),
            "password_hash": hash_password(password),
            "role": "admin",
            "identity_id": identity_id,   # maps this admin to the 'default' identity
            "created_at": _utc_now(),
            "disabled": False,
        }
    )


def get_admin_by_username(username: str) -> dict | None:
    """Find an admin by username. Returns the raw DB doc (with hash) for auth."""
    db = get_db()
    for doc in db["admin_users"].find({"disabled": {"$ne": True}}):
        try:
            if decrypt_username(doc["username_enc"]) == username:
                doc["_id"] = str(doc["_id"])
                return doc
        except Exception:
            continue
    return None


def list_admins() -> list[dict]:
    """Return all admin accounts (passwords stripped)."""
    db = get_db()
    return [_serialize(doc) for doc in db["admin_users"].find()]


# ---------------------------------------------------------------------------
# Portal users
# ---------------------------------------------------------------------------

def create_portal_user(
    username: str,
    password: str,
    features: dict[str, bool] | None = None,
    display_name: str = "",
) -> str:
    """Create a portal user. Returns the inserted _id as a string."""
    db = get_db()
    # Check for duplicate username
    for doc in db["portal_users"].find({"disabled": {"$ne": True}}):
        try:
            if decrypt_username(doc["username_enc"]) == username:
                raise ValueError(f"Username '{username}' already exists.")
        except ValueError:
            raise
        except Exception:
            continue

    merged_features = {**DEFAULT_FEATURES, **(features or {})}
    # The portal user's identity_id is their username — each user gets their
    # own isolated conversation / memory / audit namespace in MongoDB.
    result = db["portal_users"].insert_one(
        {
            "username_enc": encrypt_username(username),
            "password_hash": hash_password(password),
            "display_name": display_name,
            "identity_id": username,       # maps this user to their own identity
            "features": merged_features,
            "disabled": False,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
    )
    return str(result.inserted_id)


def get_portal_user_by_username(username: str) -> dict | None:
    """Find a portal user by username. Returns raw DB doc (with hash) for auth."""
    db = get_db()
    for doc in db["portal_users"].find({"disabled": {"$ne": True}}):
        try:
            if decrypt_username(doc["username_enc"]) == username:
                doc["_id"] = str(doc["_id"])
                return doc
        except Exception:
            continue
    return None


def get_portal_user_by_id(user_id: str) -> dict | None:
    """Return a portal user by MongoDB _id (API-safe, no hash)."""
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    db = get_db()
    doc = db["portal_users"].find_one({"_id": oid})
    return _serialize(doc) if doc else None


def list_portal_users() -> list[dict]:
    """Return all portal users (passwords stripped)."""
    db = get_db()
    return [_serialize(doc) for doc in db["portal_users"].find()]


def update_portal_user(user_id: str, patch: dict) -> bool:
    """Update editable fields on a portal user. Returns True on success."""
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    db = get_db()
    update: dict = {"$set": {"updated_at": _utc_now()}}
    if "password" in patch:
        update["$set"]["password_hash"] = hash_password(patch.pop("password"))
    if "username" in patch:
        update["$set"]["username_enc"] = encrypt_username(patch.pop("username"))
    if "display_name" in patch:
        update["$set"]["display_name"] = patch.pop("display_name")
    if "disabled" in patch:
        update["$set"]["disabled"] = bool(patch.pop("disabled"))
    result = db["portal_users"].update_one({"_id": oid}, update)
    return result.modified_count > 0


def disable_portal_user(user_id: str) -> bool:
    """Soft-disable a portal user (they can no longer log in)."""
    return update_portal_user(user_id, {"disabled": True})


def delete_portal_user(user_id: str) -> bool:
    """Hard-delete a portal user from the database."""
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    db = get_db()
    result = db["portal_users"].delete_one({"_id": oid})
    return result.deleted_count > 0


def set_user_features(user_id: str, features: dict[str, bool]) -> bool:
    """Replace the feature flags for a portal user."""
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    db = get_db()
    result = db["portal_users"].update_one(
        {"_id": oid},
        {"$set": {"features": features, "updated_at": _utc_now()}},
    )
    return result.modified_count >= 0  # 0 modified is fine if nothing changed
