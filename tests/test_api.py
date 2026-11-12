import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arcvoice import contact_id
from arcvoice.api import Settings, create_app, sign
from arcvoice.llm import KeywordClassifier

ROOT = Path(__file__).parent.parent
# CI runs this suite twice: SQLite in memory, then Postgres via DATABASE_URL
SETTINGS = Settings(
    database_url=os.environ.get("DATABASE_URL", "sqlite+pysqlite:///:memory:"),
    agents_dir=str(ROOT / "agents"),
    signal_secret="s3cret",
    tools_token="tok",
)
AUTH = {"Authorization": "Bearer tok"}


@pytest.fixture
def client():
    return TestClient(create_app(SETTINGS, classifier=KeywordClassifier()))


def post_signal(client, raw, received_at=None, secret="s3cret"):
    received_at = received_at or f"2026-11-12T21:04:00Z#{uuid.uuid4().hex[:8]}"
    body = json.dumps({"receiver": "rx-1", "raw": raw, "received_at": received_at}).encode()
    return client.post("/v1/signals", content=body, headers={"X-Signature": sign(body, secret)})


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready["agents"]["intruder-verification"] == "1.3.0"


def test_bad_signature_rejected(client):
    r = post_signal(client, contact_id.build("1234", "130"), secret="wrong")
    assert r.status_code == 401


def test_invalid_frame_rejected(client):
    assert post_signal(client, "1234181130010031").status_code == 422


def test_unknown_account_rejected(client):
    assert post_signal(client, contact_id.build("9999", "130")).status_code == 422


def test_restore_is_logged_not_called(client):
    r = post_signal(client, contact_id.build("1234", "130", qualifier="3"))
    assert r.status_code == 200 and r.json()["status"] == "logged"


def test_retransmitted_signal_is_deduplicated(client):
    raw = contact_id.build("1234", "130")
    at = f"2026-11-12T22:00:00Z#{uuid.uuid4().hex[:8]}"
    first = post_signal(client, raw, received_at=at)
    second = post_signal(client, raw, received_at=at)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "incident_id": first.json()["incident_id"]}


def test_full_call_over_http(client):
    opened = post_signal(client, contact_id.build("1234", "130", zone="001")).json()
    iid = opened["incident_id"]
    assert opened["agent"] == "intruder-verification"

    def say(text):
        r = client.post(f"/v1/incidents/{iid}/turns", json={"utterance": text}, headers=AUTH)
        assert r.status_code == 200, r.text
        return r.json()

    say("yes")
    say("bluebell")
    reply = say("someone's broken in, the door is smashed")
    assert [a["type"] for a in reply["actions"]] == ["police_dispatch"]
    final = say("yes, on my way")
    assert final["done"] and final["disposition"] == "police_dispatched"

    incident = client.get(f"/v1/incidents/{iid}", headers=AUTH).json()
    assert incident["status"] == "closed"
    assert "bluebell" not in json.dumps(incident)
    assert client.post(f"/v1/incidents/{iid}/turns", json={"utterance": "hi"}, headers=AUTH).status_code == 409

    metrics = client.get("/metrics").text
    assert 'actions_total{type="police_dispatch"} 1' in metrics
    assert 'incidents_closed_total{disposition="police_dispatched"} 1' in metrics


def test_turns_require_token(client):
    iid = post_signal(client, contact_id.build("1234", "130")).json()["incident_id"]
    assert client.post(f"/v1/incidents/{iid}/turns", json={"utterance": "yes"}).status_code == 401


def test_tool_endpoint_enforces_agent_allowlist(client):
    iid = post_signal(client, contact_id.build("5678", "301")).json()["incident_id"]
    allowed = client.post("/v1/tools/book_engineer", json={"incident_id": iid}, headers=AUTH)
    assert allowed.status_code == 200 and allowed.json()["outcome"] == "ok"
    denied = client.post("/v1/tools/request_police_dispatch", json={"incident_id": iid}, headers=AUTH)
    assert denied.status_code == 403


def test_request_id_is_echoed(client):
    r = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
