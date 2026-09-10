from __future__ import annotations

import json

import pytest

from eventmem.core import Engine
from eventmem.core.contact_tasks import ContactTaskInput, ContactTasks
from eventmem.core.db import Conflict, Deleted, Missing
from eventmem.core.mcp import create_mcp
from eventmem.core.models import ContactPolicy, Scope
from eventmem.core.scheduler import Scheduler


@pytest.fixture
def tasks(tmp_path):
    engine = Engine(tmp_path / "memory")
    scope = Scope(persona="companion")
    Scheduler(engine).policy(ContactPolicy(id="reminders", scope=scope))
    return ContactTasks(engine, scope, ["reminders"])


def task(**changes):
    return ContactTaskInput.model_validate(
        {
            "command_id": "appointment",
            "policy_id": "reminders",
            "title": "Appointment",
            "text": "The appointment starts at nine.",
            "basis": "The user requested an appointment reminder.",
            "due_at": "2099-01-01T09:00:00+08:00",
            **changes,
        }
    )


def test_lifecycle_and_idempotent_creation_report_current_state(tasks):
    request = task()
    created = tasks.create(request)
    paused = tasks.manage(created["id"], created["revision"], "pause")
    with pytest.raises(Conflict):
        tasks.manage(created["id"], created["revision"], "cancel")
    resumed = tasks.manage(created["id"], paused["revision"], "resume")
    moved = tasks.manage(
        created["id"], resumed["revision"], "snooze", "2099-01-02T09:00:00+08:00"
    )
    canceled = tasks.manage(created["id"], moved["revision"], "cancel")
    retried = tasks.create(request)
    assert retried["status"] == "canceled"
    assert retried["revision"] == canceled["revision"]
    assert retried["due_at"].startswith("2099-01-02T01:00:00")
    [item] = tasks.list()["items"]
    assert item["state"] == "canceled" and item["source_ids"] == created["source_ids"]
    source = tasks.engine.source(item["source_ids"][0])
    record = tasks.engine.get(source["record_ids"][0])
    assert record["confirmation"] == "inferred"
    assert record["attributes"]["task_basis"] == request.basis


@pytest.mark.parametrize(
    "changes",
    [
        {"due_at": "tomorrow"},
        {"due_at": "2099-01-01T09:00:00"},
        {"text": " "},
        {"basis": ""},
        {"text": "x" * 6001},
        {"recurrence": "hourly"},
    ],
)
def test_invalid_input_creates_no_memory(tasks, changes):
    with pytest.raises(ValueError):
        tasks.create(task(**changes))
    with tasks.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["missing", "scope", "trigger", "kind"])
def test_policy_validation_precedes_source_creation(tasks, change):
    policy = ContactPolicy(id="reminders", scope=tasks.scope)
    if change == "missing":
        with tasks.engine.db.connect(write=True) as conn:
            conn.execute("DELETE FROM policies")
    else:
        updates = {
            "scope": {"scope": Scope(persona="other")},
            "trigger": {"triggers": ["checkin"]},
            "kind": {"allowed_kinds": ["commitment"]},
        }[change]
        tasks.scheduler.policy(policy.model_copy(update=updates))
    with pytest.raises((Missing, Conflict)):
        tasks.create(task())
    with tasks.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


def test_scope_and_policy_filters_apply_to_list_and_control(tasks):
    created = tasks.create(task())
    other_scope = ContactTasks(tasks.engine, Scope(persona="other"), ["reminders"])
    other_policy = ContactTasks(tasks.engine, tasks.scope, ["other"])
    for manager, error in [(other_scope, Conflict), (other_policy, Missing)]:
        assert manager.list()["items"] == []
        with pytest.raises(error):
            manager.manage(created["id"], created["revision"], "cancel")
    tasks.engine.delete(created["source_ids"][0])
    assert tasks.list()["items"] == []


def test_retry_cannot_recreate_deleted_task_memory(tasks):
    created = tasks.create(task())
    source = tasks.engine.source(created["source_ids"][0])
    tasks.engine.delete(source["record_ids"][0])
    with pytest.raises(Deleted):
        tasks.create(task())
    assert tasks.list()["items"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"basis": "A different reason"},
        {"authority": "explicit"},
        {"title": "Different appointment"},
        {"due_at": "2099-01-02T09:00:00+08:00"},
        {"text": "Different content"},
    ],
)
def test_command_id_cannot_be_reused_with_changed_intent(tasks, changes):
    tasks.create(task())
    with pytest.raises(Conflict):
        tasks.create(task(**changes))
    assert len(tasks.list()["items"]) == 1
    with tasks.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_retry_finishes_interrupted_creation_without_duplicate_source(
    tasks, monkeypatch
):
    schedule = tasks.scheduler.schedule

    def interrupted(_):
        raise RuntimeError("interrupted after source receipt")

    monkeypatch.setattr(tasks.scheduler, "schedule", interrupted)
    with pytest.raises(RuntimeError):
        tasks.create(task())
    monkeypatch.setattr(tasks.scheduler, "schedule", schedule)
    created = tasks.create(task())
    assert created["status"] == "scheduled"
    with tasks.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_default_policy_produces_suggestion_not_false_delivery(tasks):
    created = tasks.create(task(due_at="2020-01-01T09:00:00+08:00"))
    [delivery] = tasks.scheduler.tick(deliver=False)["created"]
    [item] = tasks.list()["items"]
    assert item["deliveries"] == [{"id": delivery, "state": "suggested"}]
    assert item["state"] != "complete"
    canceled = tasks.manage(created["id"], item["revision"], "cancel")
    [item] = tasks.list()["items"]
    assert canceled["status"] == "canceled"
    assert item["deliveries"][0]["state"] == "canceled"


def test_invalid_reschedule_preserves_current_revision(tasks):
    created = tasks.create(task())
    for action, due in [
        ("snooze", None),
        ("snooze", "tomorrow"),
        ("cancel", "2099-01-02T09:00:00Z"),
    ]:
        with pytest.raises(ValueError):
            tasks.manage(created["id"], created["revision"], action, due)
    [item] = tasks.list()["items"]
    assert item["revision"] == created["revision"] and item["state"] == "scheduled"


@pytest.mark.asyncio
async def test_native_mcp_exposes_and_executes_task_lifecycle(tasks):
    server = create_mcp(tasks.engine)
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "recall_memory",
        "schedule_contact",
        "create_contact_task",
        "list_contact_tasks",
        "manage_contact_task",
    } <= names
    created_result = await server.call_tool(
        "create_contact_task",
        {
            "scope": tasks.scope.model_dump(),
            "task": task().model_dump(),
        },
    )
    created = json.loads(created_result[0].text)
    listed_result = await server.call_tool(
        "list_contact_tasks",
        {
            "scope": tasks.scope.model_dump(),
            "policy_ids": ["reminders"],
        },
    )
    [item] = json.loads(listed_result[0].text)["items"]
    assert item["id"] == created["id"]
    canceled_result = await server.call_tool(
        "manage_contact_task",
        {
            "scope": tasks.scope.model_dump(),
            "policy_ids": ["reminders"],
            "task_id": item["id"],
            "expected_revision": item["revision"],
            "action": "cancel",
        },
    )
    assert json.loads(canceled_result[0].text)["status"] == "canceled"
