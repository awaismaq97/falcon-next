"""
admin_auth.py — Encryption, hashing, and JWT helpers for the admin system.

Strategy:
  - Passwords:  bcrypt (one-way, slow, safe for credentials)
  - Usernames:  AES-256-GCM at rest, plus an HMAC blind index for lookup
  - Sessions:   HS256 JWT, 8-hour expiry

Key material
------------
Every key is derived from SECRET_KEY with HKDF-SHA256 under its own ``info``
label, so the JWT signing key, the username encryption key and the blind-index
key are three independent values that cannot be substituted for one another.

This replaced ``(SECRET_KEY.encode() + b"\\x00" * 32)[:32]``, which was not a key
derivation: it zero-padded a short secret to look like AES-256 while carrying
only as much entropy as the secret had, and any two secrets sharing a 32-byte
prefix produced the same key. ``LEGACY_AES_KEYS`` below still reproduces it, for
the one purpose of migrating data written under it.

Why lookup needs a blind index
------------------------------
AES-GCM uses a random nonce, so the same username encrypts to different
ciphertext every time. That makes ``username_enc`` unqueryable and unindexable,
and login used to scan the whole user collection decrypting every row to find
one match — O(n) AES operations per attempt, on an unauthenticated endpoint.

``username_index`` is a deterministic HMAC of the username. It is stored
alongside the ciphertext, uniquely indexed, and queried directly: one indexed
lookup instead of a scan, and uniqueness the database enforces rather than a
check-then-insert race. It reveals only equality — an attacker holding the
database still cannot recover a username without SECRET_KEY.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from jose import jwt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load SECRET_KEY from environment
# ---------------------------------------------------------------------------
# backend/.env is loaded here by absolute path rather than relied upon.
# falcon.config does the same load, but nothing guarantees it is imported first —
# app.deps reaches this module through falcon.admin_users, which does not touch
# config — and the pydantic fallback below resolves ".env" against the working
# directory, so it only finds the file when the server was started from
# backend/. Neither is something a missing key should depend on now that a
# missing key stops the process. override=False keeps a real environment
# variable (what the platform injects in production) ahead of the file.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(
    dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"),
    override=False,
)

_raw_secret = os.environ.get("SECRET_KEY", "").strip()
if not _raw_secret:
    # Try reading via pydantic Settings (which loads backend/.env)
    try:
        from app.settings import get_settings as _get_settings
        _raw_secret = _get_settings().secret_key.strip()
    except Exception:
        pass

MIN_SECRET_LEN = 32

# Refusing to start is the point.
#
# This used to log an error and carry on. What that produced was a server that
# looked healthy while every security property was gone: `jwt.encode(payload,
# "")` signs with the empty string, so anyone could mint themselves an admin
# token, and the AES key derived from an empty secret was 32 zero bytes — a
# value an attacker does not have to guess. An outage is visible and takes
# minutes to fix. Silent forgeable auth is neither.
if not _raw_secret or len(_raw_secret) < MIN_SECRET_LEN:
    raise RuntimeError(
        f"SECRET_KEY is missing or shorter than {MIN_SECRET_LEN} characters. "
        "Falcon will not start without it: an empty key makes every session "
        "token forgeable and every stored username decryptable by anyone.\n"
        "Set it in backend/.env or as a platform environment variable.\n"
        "Generate one with:  python -c \"import secrets; print(secrets.token_hex(32))\""
    )

SECRET_KEY: str = _raw_secret
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _derive(info: bytes, length: int = 32) -> bytes:
    """One purpose-specific key from SECRET_KEY.

    No salt: the same key has to come out on every process and every instance,
    and HKDF without a salt is defined (it substitutes a zero salt) and safe for
    a high-entropy input. The ``info`` label is what separates the purposes.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=None,
        info=info,
    ).derive(SECRET_KEY.encode("utf-8"))


_AES_KEY = _derive(b"falcon/username-encryption/v2")
_INDEX_KEY = _derive(b"falcon/username-index/v2")
# Hex so python-jose receives a str, as it did when the raw secret was passed.
_JWT_KEY = _derive(b"falcon/jwt-signing/v2").hex()

# How the AES key was built before HKDF. Kept solely so
# ``scripts/migrate_user_crypto.py`` can read rows written under it. The
# all-zeros entry covers the worst case: a deployment that ran with SECRET_KEY
# unset, whose usernames were encrypted under a key of 32 zero bytes.
LEGACY_AES_KEYS: tuple[bytes, ...] = (
    (SECRET_KEY.encode("utf-8") + b"\x00" * 32)[:32],
    b"\x00" * 32,
)


# ---------------------------------------------------------------------------
# AES-256-GCM username encryption
# ---------------------------------------------------------------------------

def _encrypt_with(key: bytes, plaintext: str) -> str:
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce — standard for GCM
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Encode nonce + ciphertext together so we can split on decrypt
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")


def _decrypt_with(key: bytes, token: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode("utf-8"))
    nonce, ciphertext = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_username(plaintext: str) -> str:
    """Encrypt a username with AES-256-GCM. Returns a base64url string."""
    return _encrypt_with(_AES_KEY, plaintext)


def decrypt_username(token: str) -> str:
    """Decrypt an AES-256-GCM username token. Raises ValueError on failure."""
    try:
        return _decrypt_with(_AES_KEY, token)
    except Exception as exc:
        raise ValueError(f"Username decryption failed: {exc}") from exc


def decrypt_username_legacy(token: str) -> str:
    """Decrypt a token written under the pre-HKDF key. Migration only.

    Tries each legacy derivation in turn. GCM authenticates, so a wrong key
    fails rather than returning plausible garbage — trying several is safe.
    """
    for key in LEGACY_AES_KEYS:
        try:
            return _decrypt_with(key, token)
        except Exception:  # noqa: BLE001 — wrong key, try the next
            continue
    raise ValueError("Username could not be decrypted with any known legacy key.")


# ---------------------------------------------------------------------------
# Blind index
# ---------------------------------------------------------------------------

def username_index(username: str) -> str:
    """A deterministic, indexable token for one username.

    Equality-preserving and nothing more: it makes ``find_one`` possible and
    lets a unique index reject duplicates, while remaining useless to anyone
    without SECRET_KEY. Whitespace is trimmed so " alice" and "alice" cannot
    become two accounts; case is preserved, matching how usernames compared
    before this existed.
    """
    return hmac.new(
        _INDEX_KEY, (username or "").strip().encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# bcrypt password hashing
# ---------------------------------------------------------------------------

def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of the plaintext password."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if plaintext matches the stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def create_access_token(user_id: str, username: str, role: str = "admin", identity_id: str = "default") -> str:
    """Create a signed JWT for the given user."""
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "identity_id": identity_id,   # which Falcon identity this user maps to
        "exp": expire,
    }
    return jwt.encode(payload, _JWT_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(token, _JWT_KEY, algorithms=[ALGORITHM])
