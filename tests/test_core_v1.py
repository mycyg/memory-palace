from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from eventmem.core import Engine, RecallRequest, SourceInput
from eventmem.core.api import create_app
from eventmem.core.db import Conflict, Deleted, Missing
from eventmem.core.jobs import Worker
from eventmem.core.models import (
    ContactPolicy,
    RecordInput,
    RevisionInput,
    ScheduleInput,
    Scope,
    now,
)
from eventmem.core.scheduler import Scheduler


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "core")


def remember(engine, text="port range allocation", key="a", **kwargs):
    source = engine.receive(SourceInput(namespace="test", key=key, text=text, **kwargs))
    return engine.source(source["id"])["record_ids"][0]


def test_idempotence_durable_receipt_and_concurrent_revisions(engine):
    source = SourceInput(namespace="test", key="same", text="A durable source")
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(lambda _: Engine(engine.db.root).receive(source), range(100))
        )
    assert len({r["id"] for r in results}) == 1
    assert engine.overview()["sources"] == 1
    assert engine.overview()["records"] == 1
    rid = engine.source(results[0]["id"])["record_ids"][0]

    def revise(i):
        try:
            return engine.revise(
                rid,
                RevisionInput(
                    expected_revision=1,
                    command_id=str(i),
                    action="correct",
                    content=f"Revision {i}",
                ),
            )
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(r is not None for r in pool.map(revise, range(10))) == 1
    assert engine.get(rid)["revision"] == 2
    with pytest.raises(Conflict):
        engine.receive(source.model_copy(update={"text": "changed without version"}))


def test_correction_invalidates_warm_cache_and_revision_reads(engine):
    rid = remember(engine)
    before = now()
    q = RecallRequest(query="allocation", budget=2000)
    assert rid in [r["id"] for r in engine.recall(q)["items"]]
    revision = RevisionInput(
        expected_revision=1,
        command_id="change",
        action="correct",
        content="Use a dedicated port allocator",
    )
    assert engine.revise(rid, revision)["revision"] == 2
    assert engine.revise(rid, revision)["revision"] == 2
    assert "dedicated" in engine.recall(RecallRequest(query="port"))["text"]
    assert engine.get(rid, known_at=before)["content"] == "port range allocation"
    engine.revise(
        rid, RevisionInput(expected_revision=2, command_id="remove", action="retract")
    )
    assert engine.recall(RecallRequest(query="port"))["items"] == []
    historical = engine.recall(
        RecallRequest(query="allocation", known_at=before, at=before)
    )
    assert rid in [r["id"] for r in historical["items"]]
    assert (
        "retracted" in engine.recall(RecallRequest(query="port", history=True))["text"]
    )


@pytest.mark.parametrize("field", ["project", "persona", "collection", "world"])
def test_scope_isolation(engine, field):
    a = remember(engine, "same overlapping text", "one")
    foreign = Scope(**{field: "other"})
    b = remember(engine, "same overlapping text", "two", scope=foreign)
    assert {
        r["id"] for r in engine.recall(RecallRequest(query="overlapping"))["items"]
    } == {a}
    assert {
        r["id"]
        for r in engine.recall(RecallRequest(query="overlapping", scope=foreign))[
            "items"
        ]
    } == {b}
    with pytest.raises(Conflict):
        engine.relate(a, "related", b)


def test_shared_preferences_require_explicit_real_scope(engine):
    from eventmem.core.retrieval import shared

    scope = shared(Scope())
    rid = remember(
        engine, "Prefer concise messages", "pref", kind="preference", scope=scope
    )
    remember(
        engine,
        "Prefer fabricated detail",
        "inferred",
        kind="preference",
        scope=scope,
        authority="model",
    )
    remember(
        engine,
        "Prefer fictional city",
        "fiction",
        kind="preference",
        scope=Scope(**(scope.model_dump() | {"world": "fiction"})),
    )
    assert [r["id"] for r in engine.recall(RecallRequest(query="Prefer"))["items"]] == [
        rid
    ]
    assert not engine.recall(RecallRequest(query="Prefer", include_shared=False))[
        "items"
    ]


def test_evidence_independence_and_generated_facts(engine):
    ids = [remember(engine, "One independent statement", str(i)) for i in range(3)]
    source_ids = [engine.get(rid)["source_ids"][0] for rid in ids]
    fact = engine.add_record(
        RecordInput(
            kind="fact",
            content="Inferred statement",
            source_ids=source_ids,
            generated=True,
        ),
        "fact",
    )
    assert fact["status"] == "unverified"
    assert engine.get(fact["id"])["independent_sources"] == 1
    diary = engine.add_record(
        RecordInput(
            kind="diary",
            content="Supported account",
            generated=True,
            source_ids=source_ids,
            evidence_ids=ids,
        ),
        "diary",
    )
    assert diary["generated"]
    engine.revise(
        ids[0],
        RevisionInput(
            expected_revision=1,
            command_id="correction",
            action="correct",
            content="Changed source statement",
        ),
    )
    assert engine.get(diary["id"])["status"] == "unverified"


def test_budget_dedup_and_compaction_across_hosts(engine):
    remember(engine, "small relevant text", "1", title="Relevant")
    engine.settings(
        "budgets", {"tool": {"startup": 100, "passive": 100, "cumulative": 100}}
    )
    q = RecallRequest(
        query="relevant", session="shared-session", phase="passive", budget=100
    )
    first = engine.recall(q)
    assert first["tokens"] <= 100 and first["items"]
    assert not Engine(engine.db.root).recall(q)["items"]
    assert engine.recall(q.model_copy(update={"phase": "read"}))["items"]
    assert engine.recall(q.model_copy(update={"phase": "compact"}))["items"]
    from eventmem.core.retrieval import tokens

    for budget in (0, 1, 10, 64, 256):
        result = engine.recall(RecallRequest(query="relevant", budget=budget))
        assert tokens(result["text"]) <= budget


def test_lease_fencing_cancellation_and_recovery(engine, monkeypatch):
    jid = engine.enqueue("test", {}, "lease")
    a, b = Worker(engine, lease_seconds=0.1), Worker(engine)
    first = a.claim()
    assert first["id"] == jid
    time.sleep(0.15)
    second = b.claim()
    assert second["fence"] == first["fence"] + 1
    with engine.db.connect(write=True) as conn:
        assert not a.owns(conn, first)
        assert b.owns(conn, second)
    b.control(jid, "cancel")
    with engine.db.connect() as conn:
        assert not b.owns(conn, second)
    parent = engine.enqueue("test", {}, "parent")
    child = engine.enqueue("test", {}, "child", [parent])
    b.control(parent, "cancel")
    assert b.claim() is None
    with engine.db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM jobs WHERE id=?", (child,)).fetchone()[0]
            == "failed"
        )


def test_model_failure_does_not_advance_progress(engine, monkeypatch):
    rid = remember(engine, "I prefer tea", extract=True)
    sid = engine.get(rid)["source_ids"][0]
    worker = Worker(engine)
    while worker.run_once():
        pass
    source = engine.source(sid)
    assert source["mechanical"] == "complete" and source["model"] == "pending"
    with engine.db.connect() as conn:
        job = conn.execute("SELECT id FROM jobs WHERE kind='extract'").fetchone()[0]
    from eventmem.core.providers import Providers

    monkeypatch.setattr(
        Providers,
        "json",
        lambda *args, **kwargs: {
            "candidates": [
                {"kind": "preference", "content": "Enjoys tea", "quote": "prefer tea"},
                {"kind": "fact", "content": "Invented", "quote": "not present"},
            ]
        },
    )
    worker.control(job, "retry")
    assert worker.run_once()
    assert engine.source(sid)["model"] == "complete"
    records = engine.list_records(Scope())["items"]
    assert any(
        r["content"] == "Enjoys tea" and r["status"] == "unverified" for r in records
    )
    assert not any(r["content"] == "Invented" for r in records)


def test_model_cancellation_during_request_prevents_commit(engine, monkeypatch):
    source = engine.receive(
        SourceInput(namespace="test", key="slow", text="Tea", extract=True)
    )
    with engine.db.connect(write=True) as conn:
        conn.execute("UPDATE jobs SET state='canceled' WHERE kind='embed'")
        jid = conn.execute("SELECT id FROM jobs WHERE kind='extract'").fetchone()[0]
    start, finish = threading.Event(), threading.Event()
    from eventmem.core.providers import Providers

    def slow(*args, **kwargs):
        start.set()
        finish.wait(5)
        return {"candidates": [{"kind": "fact", "content": "Tea fact", "quote": "Tea"}]}

    monkeypatch.setattr(Providers, "json", slow)
    worker = Worker(engine)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert start.wait(5)
    Worker(engine).control(jid, "cancel")
    finish.set()
    thread.join(5)
    assert engine.overview()["records"] == 1
    assert engine.source(source["id"])["model"] == "pending"


def test_document_versions_and_locators(engine):
    worker = Worker(engine)
    source = SourceInput(
        namespace="docs",
        key="manual",
        media_type="text/markdown",
        authority="document",
        title="Manual",
    )
    old = engine.receive(source, b"# Installation\n\nUse version one.")
    while worker.run_once():
        pass
    new = engine.receive(
        source.model_copy(update={"version": "2"}),
        b"# Installation\n\nUse version two.",
    )
    while worker.run_once():
        pass
    rows = engine.list_records(Scope())["items"]
    assert all(
        r["status"] == "superseded" for r in rows if old["id"] in r["source_ids"]
    )
    new_rows = [r for r in rows if new["id"] in r["source_ids"]]
    assert len(new_rows) == 3
    assert any(r["locator"].get("char_start") == 16 for r in new_rows)
    assert "version two" in engine.recall(RecallRequest(query="version"))["text"]


def test_deleted_sources_do_not_reappear_after_rebuild(engine):
    rid = remember(engine, "Sensitive original")
    sid = engine.get(rid)["source_ids"][0]
    engine.recall(RecallRequest(query="Sensitive", session="test"))
    engine.delete(sid)
    with pytest.raises(Missing):
        engine.get(rid)
    with pytest.raises(Missing):
        engine.source(sid)
    with pytest.raises(Deleted):
        remember(engine, "Sensitive original")
    engine.enqueue("rebuild", {}, "rebuild")
    worker = Worker(engine)
    while worker.run_once():
        pass
    assert not engine.recall(RecallRequest(query="Sensitive", history=True))["items"]


def test_contact_defaults_quiet_hours_cancellation_and_dedup(engine, monkeypatch):
    rid = remember(engine, "Remember the appointment", kind="reminder")
    scheduler = Scheduler(engine)
    due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    request = ScheduleInput(command_id="reminder", record_id=rid, due_at=due)
    first = scheduler.schedule(request)
    assert scheduler.schedule(request)["id"] == first["id"]
    delivery = scheduler.tick()["created"][0]
    with engine.db.connect() as conn:
        assert (
            conn.execute("SELECT state FROM outbox WHERE id=?", (delivery,)).fetchone()[
                0
            ]
            == "suggested"
        )
        revision = conn.execute(
            "SELECT revision FROM schedules WHERE id=?", (first["id"],)
        ).fetchone()[0]
    scheduler.control(first["id"], "cancel", revision)
    sent = []
    monkeypatch.setattr(
        httpx.Client, "post", lambda *a, **kw: sent.append(kw) or httpx.Response(200)
    )
    scheduler.policy(
        ContactPolicy(
            enabled=True,
            channel="http://localhost:9911/callback",
            require_confirmation=False,
            quiet_start=0,
            quiet_end=0,
        )
    )
    scheduler.tick()
    assert not sent
    scheduler.schedule(request.model_copy(update={"command_id": "second"}))
    scheduler.tick()
    assert len(sent) == 1
    scheduler.tick()
    assert len(sent) == 1
    assert "Idempotency-Key" in sent[0]["headers"]


def test_service_auth_origin_contract_and_cli_equivalence(engine):
    app = create_app(engine=engine, token="test-credential", workers=False)
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 401
        headers = {"Authorization": "Bearer test-credential"}
        assert (
            client.get(
                "/v1/health", headers=headers | {"Origin": "https://attacker.example"}
            ).status_code
            == 403
        )
        payload = {"namespace": "client", "key": "hello", "text": "API contract memory"}
        result = client.post("/v1/sources", headers=headers, json=payload)
        assert result.status_code == 200
        query = {"query": "contract"}
        http_result = client.post("/v1/recall", headers=headers, json=query).json()
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "eventmem.cli",
                "recall",
                "--root",
                str(engine.db.root),
                "--json",
                json.dumps(query),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        cli_result = json.loads(proc.stdout)
        assert cli_result["text"] == http_result["text"]
        assert cli_result["items"] == http_result["items"]
        schema = client.get("/v1/openapi.json", headers=headers).json()
        assert schema["info"]["version"] == "1.0.0"


def test_backup_restore_keeps_history_and_archive(engine, tmp_path):
    from eventmem.core.transfer import backup, restore

    rid = remember(engine, "Back up this memory")
    engine.revise(
        rid, RevisionInput(expected_revision=1, command_id="archive", action="archive")
    )
    archive = tmp_path / "backup.tar.gz"
    backup(engine, archive)
    target = tmp_path / "restored"
    restore(archive, target)
    other = Engine(target)
    assert other.get(rid)["status"] == "archived"
    assert len(other.history(rid)) == 2
    assert not other.recall(RecallRequest(query="Back"))["items"]
    assert other.recall(RecallRequest(query="Back", history=True))["items"]


def test_family_lifecycle_scope_and_rollback(engine):
    from eventmem.core.organize import Organizer

    ids = [remember(engine, f"topic text {i}", str(i)) for i in range(3)]
    organizer = Organizer(engine)
    family = organizer.create(Scope(), "Topic", ids)
    published = organizer.change(family["id"], 1, "publish")
    assert published["state"] == "published"
    split = organizer.change(family["id"], 2, "split", members=ids[:1], title="Subset")
    assert split["members"] == sorted(ids[1:])
    rolled = organizer.change(family["id"], 3, "rollback", target_revision=2)
    assert rolled["members"] == sorted(ids)
    assert len(organizer.graph(Scope(), family["id"])["nodes"]) == 3
