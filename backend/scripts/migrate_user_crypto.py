"""
migrate_user_crypto.py — re-encrypt usernames and build the blind index.

Run this ONCE, before deploying the code that introduced HKDF key derivation.

What changed and why it needs a migration
-----------------------------------------
``falcon.admin_auth`` used to derive its AES key by zero-padding SECRET_KEY to
32 bytes. It now derives every key with HKDF-SHA256 under a purpose label. That
is a different key, so ciphertext written under the old scheme cannot be read by
the new code: without this script every admin and portal account becomes
unreadable and nobody can log in.

The same pass adds ``username_hmac``, the deterministic blind index that replaces
the full-collection scan login used to do. A unique index is created over it.

Passwords are untouched — bcrypt hashes do not involve SECRET_KEY.

Two starting states are handled:

  * SECRET_KEY was already set — rows decrypt under the old padded derivation.
  * SECRET_KEY was never set — the server ran with an all-zeros AES key, and
    rows decrypt under that. Set a real SECRET_KEY *before* running this; the
    script reads the old rows with the zero key and writes them back under the
    new one.

Usage, from backend/ with the falcon env active::

    python -m scripts.migrate_user_crypto --dry-run   # report, change nothing
    python -m scripts.migrate_user_crypto             # apply

Idempotent: a row already carrying a ``username_hmac`` that decrypts under the
current key is skipped, so re-running is safe and a partial run can be resumed.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from falcon.admin_auth import (  # noqa: E402
    decrypt_username,
    decrypt_username_legacy,
    encrypt_username,
    username_index,
)
from falcon.db import get_db  # noqa: E402

COLLECTIONS = ("admin_users", "portal_users")


def migrate_collection(name: str, dry_run: bool) -> dict:
    db = get_db()
    coll = db[name]
    stats = {"total": 0, "already_done": 0, "migrated": 0, "unreadable": 0}
    seen: dict[str, str] = {}

    for doc in list(coll.find({})):
        stats["total"] += 1
        oid = doc["_id"]
        enc = doc.get("username_enc") or ""

        # Already migrated? Readable under the current key AND indexed.
        if doc.get("username_hmac"):
            try:
                decrypt_username(enc)
                stats["already_done"] += 1
                continue
            except ValueError:
                pass  # indexed but stale ciphertext — fall through and fix it

        username = ""
        try:
            username = decrypt_username(enc)      # new key already; only index missing
        except ValueError:
            try:
                username = decrypt_username_legacy(enc)
            except ValueError:
                stats["unreadable"] += 1
                print(f"  !! {name}/{oid}: could not decrypt under any known key — SKIPPED")
                continue

        idx = username_index(username)
        if idx in seen:
            # The unique index would reject this. Surface it now, with both ids,
            # rather than letting index creation fail with only a key to go on.
            print(
                f"  !! {name}/{oid}: duplicate username {username!r} "
                f"(also {seen[idx]}) — resolve before the unique index can be built"
            )
            stats["unreadable"] += 1
            continue
        seen[idx] = str(oid)

        print(f"  -> {name}/{oid}: {username!r}")
        stats["migrated"] += 1
        if not dry_run:
            coll.update_one(
                {"_id": oid},
                {"$set": {"username_enc": encrypt_username(username), "username_hmac": idx}},
            )
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    print(f"Database: {get_db().name}")
    print("DRY RUN — nothing will be written\n" if args.dry_run else "APPLYING CHANGES\n")

    failed = 0
    for name in COLLECTIONS:
        print(f"{name}:")
        s = migrate_collection(name, args.dry_run)
        print(
            f"  total={s['total']} already_done={s['already_done']} "
            f"migrated={s['migrated']} unreadable={s['unreadable']}\n"
        )
        failed += s["unreadable"]

    if not args.dry_run:
        print("Creating unique indexes on username_hmac…")
        for name in COLLECTIONS:
            try:
                get_db()[name].create_index("username_hmac", unique=True, sparse=True)
                print(f"  {name}: ok")
            except Exception as exc:  # noqa: BLE001
                print(f"  {name}: FAILED — {exc}")
                failed += 1

    if failed:
        print(
            f"\n{failed} problem(s). Accounts that could not be read cannot log in — "
            "resolve them (or recreate those users) before cutover."
        )
        return 1
    print("\nDone." if not args.dry_run else "\nDry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
