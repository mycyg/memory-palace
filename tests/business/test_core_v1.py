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
    ReminderPolicy,
    RecordInput,
    RevisionInput,
    ReminderInput,
    Scope,
    now,
)

from eventmem.core.scheduler import Scheduler

from eventmem.core.organize import (
    Organizer,
    prepare_events,
    prepare_summary,
    prepare_summary_part,
)
from eventmem.core.providers import Providers


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "core")


def remember(engine, text="port range allocation", key="a", **kwargs):
    source = engine.receive(SourceInput(namespace="test", key=key, text=text, **kwargs))
    return engine.source(source["id"])["record_ids"][0]


def apply_prepared(engine, prepared):
    with engine.db.connect(write=True) as conn:
        prepared(conn)
        engine.db.bump(conn)


def test_family_edit_withdraws_current_summary_without_erasing_evidence(engine):
    old = remember(engine, "Atlas staging is pending.", key="old")
    new = remember(engine, "Atlas staging is complete.", key="new")
    organizer = Organizer(engine)
    family = organizer.create(Scope(), "Atlas staging", [old], "event")
    apply_prepared(engine, prepare_summary(engine, {"family_id": family["id"]}))
    summary_id = organizer.list(Scope())[0]["summary"]["record_id"]
    original = engine.get(summary_id)

    changed = organizer.change(family["id"], 1, "revise", members=[new])
    withdrawn = engine.get(summary_id)
    assert changed["summary"]["state"] == "dirty"
    assert withdrawn["status"] == "unverified"
    assert withdrawn["source_ids"] == original["source_ids"]
    assert withdrawn["evidence_ids"] == original["evidence_ids"]
    recalled = engine.recall(RecallRequest(query="Atlas staging"))
    assert summary_id not in {item["id"] for item in recalled["items"]}
    with engine.db.connect() as conn:
        actions = [
            row[0]
            for row in conn.execute(
                "SELECT action FROM revisions WHERE record_id=? ORDER BY revision",
                (summary_id,),
            )
        ]
    assert actions == ["create", "summary_invalidated"]

    apply_prepared(engine, prepare_summary(engine, {"family_id": family["id"]}))
    refreshed = organizer.list(Scope())[0]
    current_id = refreshed["summary"]["record_id"]
    assert current_id != summary_id
    rolled_back = organizer.change(
        family["id"], refreshed["revision"], "rollback", target_revision=1
    )
    assert rolled_back["members"] == [old]
    assert engine.get(current_id)["status"] == "unverified"


def test_semantic_event_expansion_withdraws_previous_summary(engine, monkeypatch):
    first = remember(engine, "Atlas staging started.", key="event-first")

    def group(self, role, instruction, payload, **kwargs):
        return {
            "events": [
                {
                    "id": payload["candidate_events"][0]["id"]
                    if payload["candidate_events"]
                    else None,
                    "title": "Atlas staging",
                    "member_ids": [item["id"] for item in payload["records"]],
                }
            ]
        }

    monkeypatch.setattr(Providers, "json", group)
    apply_prepared(engine, prepare_events(engine, Scope()))
    organizer = Organizer(engine)
    family = organizer.list(Scope())[0]
    apply_prepared(engine, prepare_summary(engine, {"family_id": family["id"]}))
    summary_id = organizer.list(Scope())[0]["summary"]["record_id"]

    second = remember(engine, "Atlas staging completed.", key="event-second")
    apply_prepared(engine, prepare_events(engine, Scope()))
    expanded = organizer.list(Scope())[0]
    assert set(expanded["members"]) == {first, second}
    assert expanded["summary"]["state"] == "dirty"
    assert engine.get(summary_id)["status"] == "unverified"


def test_summary_part_cache_separates_event_titles(engine, monkeypatch):
    rid = remember(engine, "Atlas long task remains pending. " * 2300, key="long")
    organizer = Organizer(engine)
    alpha = organizer.create(Scope(), "Project Alpha", [rid], "event")
    beta = organizer.create(Scope(), "Project Beta", [rid], "event")

    def summary(self, role, instruction, payload, **kwargs):
        assert role == "summary"
        return {
            "content": "Summary for " + payload["title"],
            "evidence_ids": [payload["records"][0]["id"]],
        }

    monkeypatch.setattr(Providers, "json", summary)
    apply_prepared(engine, prepare_summary(engine, {"family_id": alpha["id"]}))
    with engine.db.connect() as conn:
        alpha_parts = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM jobs WHERE kind='summary_part' AND json_extract(payload,'$.family_id')=?",
                (alpha["id"],),
            )
        ]
    assert len(alpha_parts) > 1
    for part in alpha_parts:
        apply_prepared(engine, prepare_summary_part(engine, part))

    apply_prepared(engine, prepare_summary(engine, {"family_id": beta["id"]}))
    with engine.db.connect() as conn:
        beta_parts = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM jobs WHERE kind='summary_part' AND json_extract(payload,'$.family_id')=?",
                (beta["id"],),
            )
        ]
    assert len(beta_parts) == len(alpha_parts)
    alpha_ids = {part["record_id"] for part in alpha_parts}
    beta_ids = {part["record_id"] for part in beta_parts}
    assert alpha_ids.isdisjoint(beta_ids)
    apply_prepared(engine, prepare_summary_part(engine, beta_parts[0]))
    assert engine.get(beta_parts[0]["record_id"])["content"] == "Summary for Project Beta"


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
    summary = engine.add_record(
        RecordInput(
            kind="summary",
            content="Supported account",
            generated=True,
            source_ids=source_ids,
            evidence_ids=ids,
        ),
        "summary",
    )
    assert summary["generated"]
    engine.revise(
        ids[0],
        RevisionInput(
            expected_revision=1,
            command_id="correction",
            action="correct",
            content="Changed source statement",
        ),
    )
    assert engine.get(summary["id"])["status"] == "unverified"


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
    from eventmem.core.context import complete_compaction

    complete_compaction(engine, q.session, q.scope, "confirmed-compression")
    assert engine.recall(q.model_copy(update={"phase": "compact"}))["items"]
    from eventmem.core.retrieval import tokens

    for budget in (0, 1, 10, 64, 256):
        result = engine.recall(RecallRequest(query="relevant", budget=budget))
        assert tokens(result["text"]) <= budget


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
