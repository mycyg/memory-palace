"""HTTP and one-shot host boundaries for the generic work-memory service."""

import json

import pytest
from fastapi.testclient import TestClient

from eventmem.core.api import create_app
from eventmem.hooks import bridge, codex
from eventmem.sdk import DeliveryInbox


def test_generic_http_flow_and_exact_context_receipt(tmp_path):
    app = create_app(tmp_path, token="fixture", workers=False, mcp_enabled=False)
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(app) as client:
        received = client.post(
            "/v1/sources",
            headers=headers,
            json={
                "namespace": "test",
                "key": "handoff",
                "kind": "checkpoint",
                "title": "Migration checkpoint",
                "text": "The isolated migration was verified; next inspect the index.",
            },
        )
        assert received.status_code == 200
        source_id = received.json()["id"]
        listed = client.get("/v1/sources", headers=headers).json()
        assert any(row["id"] == source_id and "isolated migration" in row["excerpt"] for row in listed["items"])

        prepared = client.post(
            "/v1/recall",
            headers=headers,
            json={"session": "work-session", "query": "migration index", "phase": "startup"},
        )
        assert prepared.status_code == 200
        result = prepared.json()
        delivery = result["delivery"]
        assert delivery["state"] == "prepared"
        receipt = {
            "session": "work-session", "delivery_id": delivery["id"],
            "body_hash": delivery["body_hash"], "state": "accepted",
        }
        wrong = client.post("/v1/context/receipts", headers=headers, json={**receipt, "body_hash": "wrong"})
        assert wrong.status_code == 409
        accepted = client.post("/v1/context/receipts", headers=headers, json=receipt)
        assert accepted.status_code == 200
        assert accepted.json()["session_used"] > 0
        again = client.post("/v1/context/receipts", headers=headers, json=receipt)
        assert again.json() == accepted.json()

    paths = app.openapi()["paths"]
    assert "/v1/reminders" in paths
    assert not any(path.startswith("/v1/contact") or path.startswith("/v1/autonomy") for path in paths)


def test_one_shot_host_keeps_offline_event_without_claiming_context_acceptance(tmp_path):
    codex_result = codex.run(
        {"hook_event_name": "SessionStart", "session_id": "codex-one", "cwd": str(tmp_path)},
        root=tmp_path, url="http://127.0.0.1:1",
    )
    claude_result = bridge.run(
        "start", {"session_id": "claude-one", "cwd": str(tmp_path)},
        root=tmp_path, url="http://127.0.0.1:1",
    )
    assert codex_result == claude_result == {}
    events = [json.loads(path.read_text()) for path in (tmp_path / "host-spool").glob("*.json")]
    assert {row["payload"]["host"] for row in events} == {"codex", "claude-code"}
    assert {row["event"] for row in events} == {"start"}
    assert all(row["event"] != "context_receipt" for row in events)


def test_delivery_inbox_rejects_reused_id_with_changed_body(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite3")
    effects = []
    assert inbox.accept({"id": "delivery-1", "text": "first"}, lambda conn, delivery: effects.append(delivery))
    assert not inbox.accept({"id": "delivery-1", "text": "first"}, lambda conn, delivery: effects.append(delivery))
    with pytest.raises(ValueError, match="different body"):
        inbox.accept({"id": "delivery-1", "text": "changed"}, lambda conn, delivery: effects.append(delivery))
    assert len(effects) == 1


def test_reminder_listing_is_scoped_and_cancellation_retains_outcome(tmp_path):
    app = create_app(tmp_path, token="fixture", workers=False, mcp_enabled=False)
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(app) as client:
        ids = []
        for project in ("personal", "other-project"):
            source = client.post("/v1/sources", headers=headers, json={
                "namespace": "test-reminder", "key": project, "kind": "reminder",
                "scope": {"project": project}, "text": "Review this project's work.",
            }).json()
            record = client.get("/v1/sources/" + source["id"], headers=headers).json()["record_ids"][0]
            created = client.post("/v1/reminders", headers=headers, json={
                "command_id": "schedule-" + project, "policy_id": project, "record_id": record,
                "due_at": "2030-01-01T10:00:00Z",
            })
            assert created.status_code == 200
            ids.append(created.json()["id"])
        default = client.get("/v1/reminders/state/schedules", headers=headers).json()["items"]
        other = client.get("/v1/reminders/state/schedules", headers=headers, params={"project": "other-project"}).json()["items"]
        assert [row["id"] for row in default] == [ids[0]]
        assert [row["id"] for row in other] == [ids[1]]
        canceled = client.post("/v1/reminders/" + ids[0], headers=headers, json={
            "expected_revision": 1, "action": "cancel",
        })
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        assert canceled.json()["reconciliation_required"] is False
