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

from pymongo.errors import DuplicateKeyError

from falcon.admin_auth import (
    decrypt_username,
    encrypt_username,
    hash_password,
    username_index,
)
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
    # The Watcher Agents tab, which runs a tool directly rather than through the
    # assistant. Opt-in for the same reason as watcher: the tools it exposes act
    # on the outside world (fetch a URL, stage a tweet, delete a document), and
    # from that tab there is no model in between deciding whether to run them.
    "agents": False,
}


def merge_features(features: dict | None) -> dict[str, bool]:
    """A user's stored flags laid over the shipped defaults.

    A feature added after an account was created is absent from that account's
    stored dict, and absent has to mean "the default" rather than "unset" — for
    an opt-in feature like ``agents`` the difference decides whether every
    existing user sees a tab the admin never granted them.
    """
    return {**DEFAULT_FEATURES, **{k: bool(v) for k, v in (features or {}).items()}}


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
    out.pop("username_hmac", None)  # lookup token — never leaves the server
    out.pop("password_hash", None)  # never return the hash
    # Every flag, defaults included, so the UI can render a checkbox per feature
    # rather than only for the ones this account happens to have stored.
    if "features" in out or doc.get("role") != "admin":
        out["features"] = merge_features(doc.get("features"))
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
    try:
        db["admin_users"].insert_one(
            {
                "username_enc": encrypt_username(username),
                "username_hmac": username_index(username),
                "password_hash": hash_password(password),
                "role": "admin",
                "identity_id": identity_id,   # maps this admin to the 'default' identity
                "created_at": _utc_now(),
                "disabled": False,
            }
        )
    except DuplicateKeyError:
        # Two workers booting at once both saw an empty collection. The unique
        # index settled it; the loser has nothing to do.
        return


def get_admin_by_username(username: str) -> dict | None:
    """Find an admin by username. Returns the raw DB doc (with hash) for auth.

    One indexed lookup. This used to iterate the collection decrypting every row,
    which meant an unauthenticated login attempt cost an AES operation per
    account on the system.
    """
    doc = get_db()["admin_users"].find_one(
        {"username_hmac": username_index(username), "disabled": {"$ne": True}}
    )
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


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
    """Create a portal user. Returns the inserted _id as a string.

    Uniqueness is the unique index on ``username_hmac``, not a prior lookup. The
    check-then-insert this replaced could be raced by two concurrent creates —
    both scans found nothing, both inserted — and it only scanned enabled
    accounts, so a name could also be duplicated onto a disabled one.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("Username cannot be empty.")

    db = get_db()
    merged_features = {**DEFAULT_FEATURES, **(features or {})}
    # The portal user's identity_id is their username — each user gets their
    # own isolated conversation / memory / audit namespace in MongoDB.
    try:
        result = db["portal_users"].insert_one(
            {
                "username_enc": encrypt_username(username),
                "username_hmac": username_index(username),
                "password_hash": hash_password(password),
                "display_name": display_name,
                "identity_id": username,       # maps this user to their own identity
                "features": merged_features,
                "disabled": False,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
        )
    except DuplicateKeyError:
        raise ValueError(f"Username '{username}' already exists.") from None
    return str(result.inserted_id)


def get_portal_user_by_username(username: str) -> dict | None:
    """Find a portal user by username. Returns raw DB doc (with hash) for auth.

    One indexed lookup — see ``get_admin_by_username`` for why this is not a scan.
    """
    doc = get_db()["portal_users"].find_one(
        {"username_hmac": username_index(username), "disabled": {"$ne": True}}
    )
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


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
    """Update editable fields on a portal user.

    Returns whether the user exists, not whether anything changed. Those differ
    whenever a field is set to the value it already holds — and reporting that
    as failure made ``disable_portal_user`` return False for an account that was
    already disabled, i.e. report failure for the state the caller asked for.
    """
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    db = get_db()
    update: dict = {"$set": {"updated_at": _utc_now()}}
    if "password" in patch:
        update["$set"]["password_hash"] = hash_password(patch.pop("password"))
    if "username" in patch:
        # The blind index has to be rewritten with the ciphertext or the account
        # becomes unfindable at login — the two are one value stored twice.
        new_name = (patch.pop("username") or "").strip()
        if not new_name:
            raise ValueError("Username cannot be empty.")
        update["$set"]["username_enc"] = encrypt_username(new_name)
        update["$set"]["username_hmac"] = username_index(new_name)
        # identity_id is deliberately NOT rewritten. It is the key every
        # conversation, memory entry, document and audit record is filed under,
        # so re-pointing it on a rename would orphan the account's entire
        # history. It stays as issued; that it started life equal to the
        # username is an origin, not an invariant.
    if "display_name" in patch:
        update["$set"]["display_name"] = patch.pop("display_name")
    if "disabled" in patch:
        update["$set"]["disabled"] = bool(patch.pop("disabled"))
    try:
        result = db["portal_users"].update_one({"_id": oid}, update)
    except DuplicateKeyError:
        raise ValueError("That username is already taken.") from None
    return result.matched_count > 0


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
        {"$set": {"features": merge_features(features), "updated_at": _utc_now()}},
    )
    # matched, not modified: re-saving the same flags is a success, and
    # `modified_count >= 0` — what this used to return — is true unconditionally,
    # so the caller could never detect a bad user_id.
    return result.matched_count > 0
