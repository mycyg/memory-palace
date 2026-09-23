"""Work-memory boundaries exercised with isolated stores and deterministic model replies."""

import json
import threading
import time

import pytest

from eventmem import Engine, RecallRequest, Scope, SourceInput
from eventmem.core.context import ContextReceipt, complete_compaction, settle_context
from eventmem.core.db import Conflict, dumps
from eventmem.core.jobs import Worker
from eventmem.core.models import RecordInput, RevisionInput
from eventmem.core.organize import Organizer, prepare_events, prepare_summary
from eventmem.core.providers import ProviderError, Providers


def source(engine, key, text, **kwargs):
    receipt = engine.receive(
        SourceInput(namespace="work", key=key, text=text, **kwargs)
    )
    return engine.source(receipt["id"])["record_ids"][0]


def apply(engine, commit):
    with engine.db.connect(write=True) as conn:
        commit(conn)
        engine.db.bump(conn)


def drain(engine):
    engine.interactive_until = 0
    worker = Worker(engine)
    for _ in range(100):
        if not worker.run_once():
            break
    else:
        pytest.fail("Background work did not settle")


def model(monkeypatch):
    calls = []

    def reply(self, role, instruction, payload, **kwargs):
        calls.append((role, payload))
        if role == "organization":
            return {
                "events": [
                    {
                        "id": None,
                        "title": "Release work",
                        "member_ids": [r["id"] for r in payload["records"]],
                    }
                ]
            }
        return {
            "content": "Confirmed staging result; production remains pending.",
            "evidence_ids": list(dict.fromkeys(r["id"] for r in payload["records"])),
        }

    monkeypatch.setattr(Providers, "json", reply)
    return calls


def test_events_summaries_correction_and_stale_commit(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    calls = model(monkeypatch)
    a = source(engine, "a", "Staging accepted release alpha; production is pending.")
    source(engine, "b", "The production window for release alpha is tomorrow.")
    apply(engine, prepare_events(engine, Scope()))
    family = Organizer(engine).list(Scope())[0]
    pending = prepare_summary(engine, {"family_id": family["id"]})
    engine.revise(
        a,
        RevisionInput(
            expected_revision=1,
            command_id="correct",
            action="correct",
            content="Staging alpha failed; production remains pending.",
        ),
    )
    with pytest.raises(Conflict):
        apply(engine, pending)
    apply(engine, prepare_summary(engine, {"family_id": family["id"]}))
    family = Organizer(engine).list(Scope())[0]
    rid = family["summary"]["record_id"]
    assert family["summary"]["state"] == "ready"
    engine.revise(
        a,
        RevisionInput(
            expected_revision=2,
            command_id="correct2",
            action="correct",
            content="Staging beta passed; production remains pending.",
        ),
    )
    assert Organizer(engine).list(Scope())[0]["summary"]["state"] == "dirty"
    assert engine.get(rid)["status"] == "unverified"
    assert calls


def test_summary_batches_reuse_unchanged_parts_after_restart(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    calls = model(monkeypatch)
    a = source(engine, "large-a", "alpha task completed. " * 4000)
    b = source(engine, "large-b", "beta delivery pending. " * 3000)
    family = Organizer(engine).create(Scope(), "Large event", [a, b], "event")
    engine.enqueue("event_summary", {"family_id": family["id"]}, "summary1")
    drain(engine)
    before = len(calls)
    assert Organizer(engine).list(Scope())[0]["summary"]["state"] == "ready"
    engine = Engine(tmp_path)
    engine.revise(
        b,
        RevisionInput(
            expected_revision=1,
            command_id="change-tail",
            action="correct",
            content="beta delivery accepted. " * 3000,
        ),
    )
    engine.enqueue("event_summary", {"family_id": family["id"]}, "summary2")
    drain(engine)
    assert Organizer(engine).list(Scope())[0]["summary"]["state"] == "ready"
    assert 0 < len(calls) - before < before
    recalled = engine.recall(RecallRequest(query="staging"))
    assert all(
        not engine.get(r["id"])["attributes"].get("summary_part")
        for r in recalled["items"]
    )


def test_context_receipts_and_completed_compaction(tmp_path):
    engine = Engine(tmp_path)
    source(
        engine,
        "task",
        "Release checkpoint: staging passed, deployment is unfinished.",
        kind="checkpoint",
    )
    q = RecallRequest(query="release", session="native-task", phase="startup")
    first = engine.recall(q)
    assert first["session_used"] == 0
    d = first["delivery"]
    receipt = ContextReceipt(
        session=q.session,
        delivery_id=d["id"],
        body_hash=d["body_hash"],
        state="accepted",
    )
    with pytest.raises(Conflict):
        settle_context(engine, receipt.model_copy(update={"body_hash": "not-the-body"}))
    settle_context(engine, receipt.model_copy(update={"state": "unconfirmed"}))
    complete_compaction(engine, q.session, q.scope, "compact-1")
    complete_compaction(engine, q.session, q.scope, "compact-1")
    # A late receipt belongs to the old window and cannot consume the new budget.
    assert settle_context(engine, receipt)["session_used"] == 0
    new = Engine(tmp_path).recall(q)
    assert new["delivery"]["epoch"] == 1
    accepted = receipt.model_copy(
        update={
            "delivery_id": new["delivery"]["id"],
            "body_hash": new["delivery"]["body_hash"],
        }
    )
    assert settle_context(engine, accepted)["session_used"] == new["tokens"]
    assert settle_context(engine, accepted)["session_used"] == new["tokens"]
    assert not engine.recall(q)["items"]

    # A/B/A replacement preparations are three operations even when A repeats.
    for key in ("alpha", "beta"):
        source(engine, key, key.upper() + " deployment checkpoint.")
    replace = q.model_copy(update={"host_mode": "replace", "limit": 1})
    results = []
    for key in ("alpha", "beta", "alpha"):
        result = engine.recall(replace.model_copy(update={"query": key}))
        results.append(result)
        if len(results) < 3:
            d = result["delivery"]
            settle_context(
                engine,
                receipt.model_copy(
                    update={"delivery_id": d["id"], "body_hash": d["body_hash"]}
                ),
            )
    assert results[0]["delivery"]["id"] != results[2]["delivery"]["id"]
    d = results[0]["delivery"]
    settle_context(
        engine,
        receipt.model_copy(
            update={"delivery_id": d["id"], "body_hash": d["body_hash"]}
        ),
    )
    with engine.db.connect() as conn:
        state = json.loads(
            conn.execute(
                "SELECT data FROM sessions WHERE id=?", (q.session,)
            ).fetchone()[0]
        )
    assert state["resident"] == {r["id"]: r["revision"] for r in results[1]["items"]}
    # Exact native rejection releases reservations, without inventing acceptance.
    discarded_query = q.model_copy(
        update={"session": "discarded-input", "query": "alpha"}
    )
    prepared = engine.recall(discarded_query)
    d = prepared["delivery"]
    discard = ContextReceipt(
        session=discarded_query.session,
        delivery_id=d["id"],
        body_hash=d["body_hash"],
        state="discarded",
    )
    assert not engine.recall(discarded_query)["items"]
    assert settle_context(engine, discard)["session_used"] == 0
    assert (
        settle_context(engine, discard.model_copy(update={"state": "accepted"}))[
            "state"
        ]
        == "discarded"
    )
    assert engine.recall(discarded_query)["items"]


def test_worker_timeout_late_result_and_capacity_wait(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.settings("maintenance", {"job_timeout_seconds": 0.1, "concurrency": 1})
    jid = engine.enqueue("probe", {}, "probe")
    released, preparing = threading.Event(), threading.Event()

    def slow(job):
        preparing.set()
        released.wait(2)
        return lambda conn: conn.execute(
            "INSERT INTO settings VALUES('late-result','true')"
        )

    worker = Worker(engine)
    monkeypatch.setattr(worker, "prepare", slow)
    attempt = threading.Thread(target=worker.run_once)
    attempt.start()
    try:
        assert preparing.wait(1)
        attempt.join(0.15)
        assert attempt.is_alive()
        engine.enqueue("probe", {}, "other-probe")
        assert Worker(engine).claim() is None
    finally:
        released.set()
        attempt.join(2)
    assert not attempt.is_alive()
    with engine.db.connect() as conn:
        row = conn.execute(
            "SELECT state,attempts FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        assert tuple(row) == ("retry", 1)
        assert not conn.execute(
            "SELECT 1 FROM settings WHERE key='late-result'"
        ).fetchone()
    # Waiting for foreground work consumes no attempt.
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?)",
            ("busy", Scope().key(), dumps({"foreground_until": time.time() + 60})),
        )
        conn.execute("UPDATE jobs SET available=0 WHERE id=?", (jid,))
    assert worker.claim() is None
    with engine.db.connect() as conn:
        assert (
            conn.execute("SELECT attempts FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            == 1
        )


def test_provider_truncation_is_one_failed_attempt(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    engine.settings(
        "models", {"summary": {"endpoint": "http://localhost/v1", "model": "any-model"}}
    )
    calls = []

    def truncated(*args, **kwargs):
        calls.append(1)
        return {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": '{"content":"partial"}'},
                }
            ]
        }

    monkeypatch.setattr(Providers, "request", truncated)
    with pytest.raises(ProviderError, match="budget"):
        Providers(engine).json("summary", "Summarize", {})
    assert len(calls) == 1


def test_provider_deadline_includes_local_model_startup(tmp_path, monkeypatch):
    from eventmem.core import local_embedding
    from eventmem.core.providers import request_deadline

    engine = Engine(tmp_path)
    engine.settings(
        "models",
        {
            "embedding": {
                "endpoint": "http://localhost/v1",
                "model": "fixture",
                "local_embedding": True,
            }
        },
    )
    clock = [0.0]

    def wake(*args):
        clock[0] = 2.0
        return "fixture-token"

    monkeypatch.setattr(local_embedding, "ensure_started", wake)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "httpx.Client", lambda **kw: pytest.fail("Expired attempt opened a request")
    )
    token = request_deadline.set(1.0)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            Providers(engine).request("embedding", "embeddings", json_={})
    finally:
        request_deadline.reset(token)


def test_procedure_counterexample_rechecks_and_real_usage_dedup(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    rid = source(
        engine,
        "method",
        "Retry with the same operation ID after confirming the first attempt was not sent.",
        kind="procedure",
    )
    outcome = source(
        engine,
        "result",
        "Unknown delivery was retried and produced duplicate effects.",
        kind="episode",
        authority="operation",
    )
    engine.relate(rid, "counterexample", outcome)
    assert engine.get(rid)["attributes"]["review_required"]
    assert (
        "review required" in engine.recall(RecallRequest(query="retry"))["text"].lower()
    )

    def review(*args, **kwargs):
        return {
            "applicability": "Use only after confirmed non-delivery.",
            "limitations": "Unknown delivery must be reconciled.",
            "evidence_ids": [outcome],
        }

    monkeypatch.setattr(Providers, "json", review)
    drain(engine)
    assert not engine.get(rid)["attributes"]["review_required"]
    assert engine.get(rid)["attributes"]["review_basis"] == "model_inference"
    assert (
        "Unknown delivery must be reconciled."
        in engine.recall(RecallRequest(query="retry"))["text"]
    )
    engine.revise(
        outcome,
        RevisionInput(
            expected_revision=1,
            command_id="result-correction",
            action="correct",
            content="The receiver actually deduplicated the retry.",
        ),
    )
    assert engine.get(rid)["attributes"]["review_required"]
    engine.feedback(rid, "read", "input-1")
    engine.feedback(rid, "verified", "input-1")
    engine.recall(RecallRequest(query="retry", session="passive", phase="passive"))
    with engine.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE record_id=?", (rid,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("kind", ["knowledge", "observation"])
def test_source_revision_invalidates_derived_summary(tmp_path, kind):
    engine = Engine(tmp_path)
    a = source(
        engine,
        "document",
        "Release A passed staging.",
        kind=kind,
        version="1",
        occurred_at="2026-01-01T00:00:00Z",
    )
    summary = engine.add_record(
        RecordInput(
            kind="summary",
            content="Release A passed.",
            source_ids=engine.get(a)["source_ids"],
            evidence_ids=[a],
            generated=True,
        ),
        "derived",
    )
    source(
        engine,
        "document",
        "Correction: release A failed staging.",
        kind=kind,
        version="2",
        occurred_at="2026-01-02T00:00:00Z",
    )
    assert engine.get(a)["status"] == "superseded"
    assert engine.get(summary["id"])["status"] == "unverified"


def test_summary_deletion_covers_uncited_model_input(tmp_path, monkeypatch):
    from eventmem.core.db import Missing

    engine = Engine(tmp_path)
    first = source(engine, "visible-input", "Release notes say staging passed.")
    private = source(engine, "private-input", "Private code: blue-bird-731.")
    family = Organizer(engine).create(
        Scope(), "Release event", [first, private], "event"
    )

    def incomplete_citations(self, role, instruction, payload, **kwargs):
        assert role == "summary"
        assert {piece["id"] for piece in payload["records"]} == {first, private}
        return {
            "content": "Staging passed. Private code: blue-bird-731.",
            "evidence_ids": [first],
        }

    monkeypatch.setattr(Providers, "json", incomplete_citations)
    apply(engine, prepare_summary(engine, {"family_id": family["id"]}))
    summary_id = Organizer(engine).list(Scope())[0]["summary"]["record_id"]
    summary = engine.get(summary_id)
    assert set(summary["evidence_ids"]) == {first, private}
    assert set(summary["source_ids"]) == set(
        engine.get(first)["source_ids"] + engine.get(private)["source_ids"]
    )
    assert summary["attributes"]["cited_ids"] == [first]

    engine.delete(private)
    with pytest.raises(Missing):
        engine.get(summary_id)
    assert engine.get(first)["content"] == "Release notes say staging passed."


def test_delete_keeps_unrelated_receipts_and_scrubs_pending_summary_text(tmp_path):
    from eventmem.core.db import Missing

    engine = Engine(tmp_path)
    a = source(engine, "private-a", "Private planning detail A.")
    b = source(engine, "kept-b", "Release B remains unfinished.")
    family = Organizer(engine).create(Scope(), "Event A", [a], "event")
    jid = engine.enqueue(
        "summary_part",
        {
            "family_id": family["id"],
            "record_id": "not-created",
            "pieces": [{"id": a, "content": "Private planning detail A."}],
        },
        "pending-summary",
    )
    q = RecallRequest(query="release", session="kept-session", phase="startup")
    prepared = engine.recall(q)
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE jobs SET state='failed',error=? WHERE id=?",
            ("Invalid input: Private planning detail A.", jid),
        )
    Worker(engine).recover([jid], command_id="retry-summary")
    engine.delete(a)
    with pytest.raises(Missing):
        engine.get(a)
    assert engine.get(b)["content"] == "Release B remains unfinished."
    with engine.db.connect() as conn:
        job = conn.execute(
            "SELECT state,payload FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        assert tuple(job) == ("canceled", "{}")
        assert (
            conn.execute(
                "SELECT target FROM job_recovery WHERE job_id=?", (jid,)
            ).fetchone()[0]
            == "[deleted]"
        )
        assert (
            conn.execute(
                "SELECT prev_error FROM job_recovery WHERE job_id=?", (jid,)
            ).fetchone()[0]
            is None
        )
    d = prepared["delivery"]
    accepted = settle_context(
        engine,
        ContextReceipt(
            session=q.session,
            delivery_id=d["id"],
            body_hash=d["body_hash"],
            state="accepted",
        ),
    )
    assert accepted["session_used"] == prepared["tokens"]


def test_event_grouping_rechecks_candidate_evidence(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    existing = source(engine, "existing", "Task alpha deliverable approved.")
    family = Organizer(engine).create(Scope(), "Task alpha", [existing], "event")
    fresh = source(engine, "incoming", "Task alpha next step.")

    def reply(*args, **kwargs):
        return {
            "events": [
                {"id": family["id"], "title": "Task alpha", "member_ids": [fresh]}
            ]
        }

    monkeypatch.setattr(Providers, "json", reply)
    commit = prepare_events(engine, Scope())
    engine.revise(
        existing,
        RevisionInput(
            expected_revision=1,
            command_id="candidate-correction",
            action="correct",
            content="Correction: this was an unrelated task beta.",
        ),
    )
    with pytest.raises(Conflict):
        apply(engine, commit)
    assert Organizer(engine).list(Scope())[0]["members"] == [existing]


def test_summary_serialized_batch_budget(tmp_path):
    from eventmem.core.organize import summary_chunks
    from eventmem.core.retrieval import tokens

    engine = Engine(tmp_path)
    rid = source(engine, "escapes", ('"\\\n' * 12000))
    chunks = summary_chunks([engine.get(rid)])
    assert len(chunks) > 1
    assert all(tokens(dumps(chunk)) <= 6000 for chunk in chunks)


def test_source_identity_includes_provenance_changes(tmp_path):
    engine = Engine(tmp_path)
    source = SourceInput(
        namespace="adapter", key="one", text="Task completed.", authority="model"
    )
    first = engine.receive(source)
    assert engine.receive(source)["id"] == first["id"]
    with pytest.raises(Conflict, match="new version"):
        engine.receive(source.model_copy(update={"authority": "operation"}))
    with pytest.raises(Conflict, match="new version"):
        engine.receive(source.model_copy(update={"metadata": {"result": "accepted"}}))


def test_conflict_proposal_rechecks_targets_and_flags_procedures(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    method = source(
        engine,
        "method",
        "Retry uploads after confirming non-delivery.",
        kind="procedure",
    )
    result = source(engine, "result", "Retry caused two uploads.")
    monkeypatch.setattr(
        Providers,
        "json",
        lambda *a, **kw: {
            "relations": [
                {"id": method, "relation": "refutes", "reason": "Duplicate upload"}
            ]
        },
    )
    job = {"kind": "conflict", "payload": dumps({"record_id": result})}
    commit = Worker(engine).prepare(job)
    engine.revise(
        method,
        RevisionInput(
            expected_revision=1,
            command_id="correct-method",
            action="correct",
            content="Never retry an upload whose acceptance is unknown.",
        ),
    )
    with pytest.raises(Conflict, match="evidence changed"):
        apply(engine, commit)
    with engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM relations").fetchone()
    apply(engine, Worker(engine).prepare(job))
    updated = engine.get(method)
    assert updated["attributes"]["review_required"]
    # An identical relation has no new evidence and must not schedule another review.
    apply(engine, Worker(engine).prepare(job))
    assert engine.get(method)["revision"] == updated["revision"]
