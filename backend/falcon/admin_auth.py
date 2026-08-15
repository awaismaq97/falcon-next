"""
admin_auth.py — Encryption, hashing, and JWT helpers for the admin system.

Strategy:
  - Passwords:  bcrypt (one-way, slow, safe for credentials)
  - Usernames:  AES-256-GCM (reversible — needed for lookup after encryption)
  - Sessions:   HS256 JWT signed with SECRET_KEY, 8-hour expiry

SECRET_KEY must be set in the environment. A missing or weak key is logged as
an error at import time but does NOT crash the server, so startup error logs are
visible on the hosting platform.
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load SECRET_KEY from environment
# ---------------------------------------------------------------------------
_raw_secret = os.environ.get("SECRET_KEY", "").strip()
if not _raw_secret:
    # Try reading via pydantic Settings (which loads backend/.env)
    try:
        from app.settings import get_settings as _get_settings
        _raw_secret = _get_settings().secret_key.strip()
    except Exception:
        pass

if not _raw_secret or len(_raw_secret) < 32:
    logger.error(
        "SECRET_KEY is not set or is shorter than 32 characters. "
        "Set a strong random SECRET_KEY environment variable before running in production."
    )

SECRET_KEY: str = _raw_secret
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8


# ---------------------------------------------------------------------------
# AES-256-GCM username encryption
# ---------------------------------------------------------------------------

def _derive_aes_key() -> bytes:
    """Derive a 32-byte AES key from SECRET_KEY using UTF-8 encoding + zero-padding."""
    raw = SECRET_KEY.encode("utf-8")
    # Pad / truncate to exactly 32 bytes (AES-256)
    return (raw + b"\x00" * 32)[:32]


def encrypt_username(plaintext: str) -> str:
    """Encrypt a username with AES-256-GCM. Returns a base64url string."""
    key = _derive_aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce — standard for GCM
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Encode nonce + ciphertext together so we can split on decrypt
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("utf-8")


def decrypt_username(token: str) -> str:
    """Decrypt an AES-256-GCM username token. Raises ValueError on failure."""
    key = _derive_aes_key()
    aesgcm = AESGCM(key)
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8"))
        nonce, ciphertext = raw[:12], raw[12:]
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Username decryption failed: {exc}") from exc


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
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
