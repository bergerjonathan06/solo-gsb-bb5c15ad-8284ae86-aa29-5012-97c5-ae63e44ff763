"""API integration tests (SQLite-backed, idempotency and queries)."""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest

# Point the database at a temporary file BEFORE importing the app.
_TMP_DIR = tempfile.mkdtemp()
os.environ["RESOLVER_DB"] = os.path.join(_TMP_DIR, "test.db")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from app import db

    db.init_db()
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


PAYLOAD = {
    "components": [
        {
            "name": "app",
            "candidates": [
                {"version": "1.0.0", "dependencies": {"lib": "^1.0.0"}}
            ],
        },
        {"name": "lib", "candidates": [{"version": "1.0.0"}, {"version": "1.5.0"}]},
    ]
}

CONFLICT_PAYLOAD = {
    "components": [
        {"name": "a", "candidates": [{"version": "1.0.0", "dependencies": {"x": "^2.0.0"}}]},
        {"name": "b", "candidates": [{"version": "1.0.0", "dependencies": {"x": "~1.0.0"}}]},
        {"name": "x", "candidates": [{"version": "1.0.0"}, {"version": "2.0.0"}]},
    ],
    "roots": ["a", "b"],
}


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_resolve_and_idempotent_reuse(client):
    resp1 = client.post("/api/v1/resolve", json=PAYLOAD)
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["status"] == "satisfiable"
    assert body1["cached"] is False
    assert body1["hit_count"] == 1
    assert body1["selection"]["lib"] == "1.5.0"
    fingerprint = body1["fingerprint"]
    assert len(fingerprint) == 64

    # Same semantic request, reordered -> identical fingerprint, cache hit.
    reordered = {
        "components": list(reversed(PAYLOAD["components"])),
    }
    resp2 = client.post("/api/v1/resolve", json=reordered)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["fingerprint"] == fingerprint
    assert body2["cached"] is True
    assert body2["hit_count"] == 2

    # GET by fingerprint returns the stored result without touching counters.
    resp3 = client.get(f"/api/v1/resolutions/{fingerprint}")
    assert resp3.status_code == 200
    assert resp3.json()["hit_count"] == 2


def test_get_unknown_fingerprint_404(client):
    resp = client.get("/api/v1/resolutions/" + "0" * 64)
    assert resp.status_code == 404


def test_conflict_response(client):
    resp = client.post("/api/v1/resolve", json=CONFLICT_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unsatisfiable"
    conflict = body["conflict"]
    assert conflict["code"] == "no_candidate"
    assert conflict["component"] == "x"
    assert len(conflict["minimal_constraints"]) == 2
    assert len(conflict["chains"]) == 2


def test_validation_error_422(client):
    resp = client.post(
        "/api/v1/resolve",
        json={"components": [{"name": "a", "candidates": [{"version": "bad"}]}]},
    )
    assert resp.status_code == 422
    assert "semver" in resp.json()["detail"]


def test_list_endpoint(client):
    client.post("/api/v1/resolve", json=PAYLOAD)
    resp = client.get("/api/v1/resolutions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    assert all("fingerprint" in item for item in body["items"])


def test_different_prefer_distinct_fingerprint(client):
    low = dict(PAYLOAD, prefer="lowest")
    resp = client.post("/api/v1/resolve", json=low)
    body = resp.json()
    assert body["selection"]["lib"] == "1.0.0"
    assert body["cached"] is False
