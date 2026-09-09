"""
tests/test_admin_crypto.py — key derivation, the blind index, and user lookup.

Run with:
    conda run -n falcon pytest tests/test_admin_crypto.py -v
"""
from __future__ import annotations

import os
import secrets
import sys

import mongomock
import pymongo
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
pymongo.MongoClient = mongomock.MongoClient  # type: ignore[assignment]

from falcon import admin_users as Users  # noqa: E402
from falcon.admin_auth import (  # noqa: E402
    _AES_KEY,
    _INDEX_KEY,
    LEGACY_AES_KEYS,
    SECRET_KEY,
    create_access_token,
    decode_access_token,
    decrypt_username,
    decrypt_username_legacy,
    encrypt_username,
    username_index,
)
from falcon.db import get_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_users():
    db = get_db()
    db["portal_users"].delete_many({})
    db["admin_users"].delete_many({})
    # mongomock does not run the background index build, so the uniqueness the
    # store now relies on has to be declared here for these tests to mean anything.
    db["portal_users"].create_index("username_hmac", unique=True, sparse=True)
    db["admin_users"].create_index("username_hmac", unique=True, sparse=True)
    yield


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def test_keys_are_independent_of_each_other():
    """Three purposes, three keys. Reusing one for another is the classic misuse."""
    assert _AES_KEY != _INDEX_KEY
    assert _AES_KEY != SECRET_KEY.encode("utf-8")


def test_derivation_is_not_the_old_zero_padding():
    """The old scheme carried only the secret's own entropy, zero-padded to look
    like AES-256, and collided for any two secrets sharing a 32-byte prefix."""
    assert _AES_KEY != (SECRET_KEY.encode("utf-8") + b"\x00" * 32)[:32]


def test_encryption_round_trips():
    for name in ("alice", "Ünïcodé user", "a" * 200, "x"):
        assert decrypt_username(encrypt_username(name)) == name


def test_ciphertext_is_randomised():
    """Which is exactly why lookup needs the blind index rather than a query on
    the ciphertext."""
    assert encrypt_username("alice") != encrypt_username("alice")


def test_legacy_ciphertext_is_readable_for_migration():
    """The migration has to read what the old derivation wrote."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    for key in LEGACY_AES_KEYS:
        nonce = os.urandom(12)
        import base64

        blob = base64.urlsafe_b64encode(
            nonce + AESGCM(key).encrypt(nonce, b"legacy_user", None)
        ).decode()
        assert decrypt_username_legacy(blob) == "legacy_user"
        # And the current key must NOT read it — otherwise nothing needed migrating.
        with pytest.raises(ValueError):
            decrypt_username(blob)


def test_jwt_round_trips():
    claims = decode_access_token(create_access_token("u1", "alice", "user", "alice"))
    assert claims["sub"] == "u1"
    assert claims["identity_id"] == "alice"
    assert claims["role"] == "user"


def test_a_token_signed_with_the_raw_secret_is_rejected():
    """The signing key is derived too, so the bare secret is not a valid signer."""
    from jose import jwt, JWTError

    forged = jwt.encode({"sub": "x", "role": "admin"}, SECRET_KEY, algorithm="HS256")
    with pytest.raises(JWTError):
        decode_access_token(forged)


# ---------------------------------------------------------------------------
# Blind index
# ---------------------------------------------------------------------------

def test_index_is_deterministic_and_distinguishing():
    assert username_index("alice") == username_index("alice")
    assert username_index("alice") != username_index("bob")


def test_index_trims_surrounding_whitespace():
    """Otherwise " alice" and "alice" are two accounts that look like one."""
    assert username_index(" alice ") == username_index("alice")


def test_index_does_not_reveal_the_username():
    idx = username_index("alice")
    assert "alice" not in idx
    assert len(idx) == 64  # hex sha256


# ---------------------------------------------------------------------------
# User store
# ---------------------------------------------------------------------------

def test_create_then_find_by_username():
    uid = Users.create_portal_user("alice", "pw")
    found = Users.get_portal_user_by_username("alice")
    assert found and found["_id"] == uid
    assert Users.get_portal_user_by_username("bob") is None


def test_duplicate_username_is_refused_by_the_index():
    Users.create_portal_user("alice", "pw")
    with pytest.raises(ValueError, match="already exists"):
        Users.create_portal_user("alice", "other")


def test_duplicate_is_refused_even_when_the_original_is_disabled():
    """The old scan only looked at enabled accounts, so this slipped through."""
    uid = Users.create_portal_user("alice", "pw")
    Users.disable_portal_user(uid)
    with pytest.raises(ValueError, match="already exists"):
        Users.create_portal_user("alice", "other")


def test_rename_keeps_the_account_findable():
    """The ciphertext and the index are one value stored twice; updating only
    the first would leave the account unable to log in under either name."""
    uid = Users.create_portal_user("alice", "pw")
    assert Users.update_portal_user(uid, {"username": "alicia"})
    assert Users.get_portal_user_by_username("alice") is None
    assert Users.get_portal_user_by_username("alicia")["_id"] == uid


def test_rename_does_not_orphan_the_account_data():
    """identity_id is the key every message, memory and document is filed under."""
    uid = Users.create_portal_user("alice", "pw")
    Users.update_portal_user(uid, {"username": "alicia"})
    assert Users.get_portal_user_by_id(uid)["identity_id"] == "alice"


def test_serialised_user_never_carries_the_lookup_token_or_the_hash():
    uid = Users.create_portal_user("alice", "pw")
    out = Users.get_portal_user_by_id(uid)
    assert out["username"] == "alice"
    assert "username_hmac" not in out
    assert "username_enc" not in out
    assert "password_hash" not in out


def test_update_reports_whether_the_user_exists():
    uid = Users.create_portal_user("alice", "pw")
    # Setting a value it already holds is a success, not a failure.
    assert Users.disable_portal_user(uid) is True
    assert Users.disable_portal_user(uid) is True
    assert Users.update_portal_user("aaaaaaaaaaaaaaaaaaaaaaaa", {"display_name": "x"}) is False


def test_set_features_reports_a_bad_user_id():
    """It used to return `modified_count >= 0`, which is true unconditionally."""
    uid = Users.create_portal_user("alice", "pw")
    assert Users.set_user_features(uid, {"agents": True}) is True
    assert Users.set_user_features("aaaaaaaaaaaaaaaaaaaaaaaa", {"agents": True}) is False


def test_seed_first_admin_is_idempotent():
    Users.seed_first_admin("root", "pw")
    Users.seed_first_admin("root", "pw")
    Users.seed_first_admin("other", "pw")
    assert get_db()["admin_users"].count_documents({}) == 1
    assert Users.get_admin_by_username("root") is not None
