"""
tests/test_api_auth.py — the API's authentication and authorisation boundary.

These tests enumerate the live OpenAPI schema rather than a hand-written list of
endpoints, which is the whole point of them. The hole they exist to prevent was
not a route with the wrong check on it — it was fifty-five routes with no check
at all, because auth was opt-in per handler and nobody had opted in. A test that
named the endpoints it knew about would have missed exactly the ones that were
forgotten.

So: every operation in the schema must reject an anonymous caller, and every
operation that names an identity must reject a caller asking for someone else's.
A route added tomorrow is covered the moment it is registered.

Run with:
    conda run -n falcon pytest tests/test_api_auth.py -v
"""
from __future__ import annotations

import os
import secrets
import sys

import mongomock
import pymongo
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both must exist before the app imports: falcon.admin_auth refuses to load
# without a SECRET_KEY, which is itself one of the behaviours under test.
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

# Keep every database call in memory. Authorisation is decided before the
# handlers reach Mongo, but a refused request must not be indistinguishable from
# one that merely failed to connect.
pymongo.MongoClient = mongomock.MongoClient  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from falcon.admin_auth import create_access_token  # noqa: E402

# Routes that must stay reachable without a token, and why.
PUBLIC = {
    "/health",                                        # liveness probe
    "/api/admin/login",                               # issues the token
    "/api/identities/{identity_id}/watcher/stream",   # EventSource; checks its own
}

# Placeholders filled in to make a concrete URL. The values are deliberately
# someone else's — "bob" is never the caller in these tests.
SAMPLE = {
    "{identity_id}": "bob",
    "{memory_id}": "aaaaaaaaaaaaaaaaaaaaaaaa",
    "{storage_id}": "doc_aaaaaaaaaaaa",
    "{user_ts}": "2026-01-01T00:00:00Z",
    "{category_id}": "c1",
    "{message_id}": "m1",
    "{name}": "some_tool",
    "{code}": "abcd",
    "{job_id}": "j1",
    "{user_id}": "aaaaaaaaaaaaaaaaaaaaaaaa",
    "{record_id}": "r1",
}

METHODS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def client(app):
    # No context manager: the lifespan starts watcher threads and a research
    # worker, none of which these tests exercise.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def operations(app):
    """Every (path, method) the app publishes, from the live schema."""
    out = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if method in METHODS:
                out.append((path, method))
    return out


def concrete(path: str) -> str:
    for placeholder, value in SAMPLE.items():
        path = path.replace(placeholder, value)
    return path


def token(identity: str, role: str = "user") -> dict:
    return {
        "Authorization": "Bearer "
        + create_access_token(f"u-{identity}", identity, role=role, identity_id=identity)
    }


def test_schema_is_not_empty(operations):
    """Guards the enumeration itself.

    Routers are materialised lazily, so a mistake here could leave `operations`
    empty and make every test below pass by testing nothing.
    """
    assert len(operations) > 50, f"only {len(operations)} operations found"


def test_every_operation_requires_a_token(client, operations):
    leaks = []
    for path, method in operations:
        if path in PUBLIC:
            continue
        r = client.request(method.upper(), concrete(path), json={})
        if r.status_code != 401:
            leaks.append(f"{r.status_code} {method.upper()} {path}")
    assert not leaks, "reachable without a token:\n  " + "\n  ".join(sorted(leaks))


def test_no_identity_route_serves_a_foreign_identity(client, operations):
    """A portal user asking for someone else's identity must be refused.

    422 counts as refused: the request body failed validation, so the handler
    never ran and no data was returned either way.
    """
    headers = token("alice")
    leaks = []
    for path, method in operations:
        if "{identity_id}" not in path or path in PUBLIC:
            continue
        r = client.request(method.upper(), concrete(path), json={}, headers=headers)
        if r.status_code not in (403, 422):
            leaks.append(f"{r.status_code} {method.upper()} {path} -> {r.text[:60]}")
    assert not leaks, "reached another identity:\n  " + "\n  ".join(sorted(leaks))


def test_admin_may_reach_another_identity(client):
    """The override the admin UI depends on — identity switching in the sidebar."""
    r = client.get("/api/identities/bob/history", headers=token("root", role="admin"))
    assert r.status_code == 200


def test_portal_user_sees_only_their_own_identity(client):
    """Identity ids are usernames, so the full list is a user directory."""
    r = client.get("/api/identities", headers=token("alice"))
    assert r.status_code == 200
    assert [i["identity_id"] for i in r.json()["identities"]] == ["alice"]


def test_documents_do_not_default_to_every_account(client):
    """An omitted identity used to mean "no filter" — every user's files.

    It now means "my own", so this returns alice's documents (none) rather than
    the contents of the whole collection.
    """
    r = client.get("/api/documents/stored", headers=token("alice"))
    assert r.status_code == 200
    assert r.json()["documents"] == []


def test_document_store_refuses_an_unscoped_query():
    """The store layer refuses too, so the guarantee does not rest on the router."""
    from falcon import documents_store as Store

    for call in (
        lambda: Store.list_documents(""),
        lambda: Store.get("doc_aaaaaaaaaaaa", ""),
        lambda: Store.delete("doc_aaaaaaaaaaaa", ""),
        lambda: Store.search("", "anything"),
    ):
        with pytest.raises(ValueError, match="identity_id is required"):
            call()


def test_debug_env_is_admin_only_and_lists_no_variable_names(client):
    assert client.get("/debug-env").status_code == 401
    assert client.get("/debug-env", headers=token("alice")).status_code == 403

    r = client.get("/debug-env", headers=token("root", role="admin"))
    assert r.status_code == 200
    assert "all_keys" not in r.json()


def test_watcher_stream_refuses_an_anonymous_or_foreign_listener(client):
    """It used to stream on a missing or unparseable token — `pass  # allow through`."""
    assert client.get("/api/identities/bob/watcher/stream").status_code == 401
    assert client.get(
        "/api/identities/bob/watcher/stream?token=not-a-jwt"
    ).status_code == 401

    alice = create_access_token("u1", "alice", role="user", identity_id="alice")
    assert client.get(
        f"/api/identities/bob/watcher/stream?token={alice}"
    ).status_code == 403
