"""
admin_audit.py — Admin action audit log.

Separate from the AI inference audit (falcon/audit.py). This logs every
admin action: user creation, permission changes, password resets, logins, etc.

Collection: admin_audit_log
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from falcon.db import get_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_admin_action(
    actor: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Insert one admin audit record.

    Args:
        actor:   Username of the admin performing the action.
        action:  Short action label, e.g. 'create_user', 'update_permissions'.
        target:  Resource affected (e.g. target username or user_id). Optional.
        details: Any extra key/value context.
    """
    db = get_db()
    db["admin_audit_log"].insert_one(
        {
            "timestamp": _utc_now(),
            "actor": actor,
            "action": action,
            "target": target,
            "details": details or {},
        }
    )


def list_admin_audit(limit: int = 200) -> list[dict]:
    """Return recent admin audit records, newest-first."""
    db = get_db()
    cursor = (
        db["admin_audit_log"]
        .find({}, {"_id": 0})
        .sort("timestamp", -1)
        .limit(limit)
    )
    return list(cursor)
