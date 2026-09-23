from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import Scope
from eventmem.core.transfer import migrate

STAMP = "2026-01-02T03:04:05+00:00"


def _old_sqlite(root: Path) -> tuple[str, str]:
    root.mkdir()
    blobs = root / "blobs"
    blobs.mkdir()
    raw = b"original attachment\n"
    key = digest(raw)
    (blobs / key).write_bytes(raw)
    source_id, record_id = "src_original", "mem_original"
    source_data = {
        "namespace": "legacy", "key": "item", "version": "7", "scope": Scope().model_dump(),
        "session": "old-session", "occurred_at": STAMP, "media_type": "application/octet-stream",
        "title": "Original source", "origin": "", "kind": "observation", "authority": "document",
        "extract": False, "metadata": {}, "byte_length": len(raw),
    }
    base = {
        "id": record_id, "kind": "episode", "title": "Old title", "content": "first version",
        "scope": Scope().model_dump(), "source_ids": [source_id], "evidence_ids": [],
        "parent_id": None, "valid_from": STAMP, "valid_until": None, "status": "active",
        "confirmation": "documented", "importance": .5, "generated": False,
        "attributes": {}, "locator": {}, "revision": 1, "received_at": STAMP,
        "updated_at": STAMP, "read_url": f"/v1/memories/{record_id}",
        "instruction_authority": "data",
    }
    second = dict(base, content="corrected version", revision=2)
    with sqlite3.connect(root / "memory.sqlite3") as conn:
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            CREATE TABLE sources(id TEXT PRIMARY KEY,namespace TEXT,source_key TEXT,version TEXT,
                scope TEXT,session TEXT,received_at TEXT,occurred_at TEXT,hash TEXT,blob TEXT,
                data TEXT,mechanical TEXT,model TEXT,deleted INTEGER DEFAULT 0);
            CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT,scope TEXT,status TEXT,valid_from TEXT,
                valid_until TEXT,received_at TEXT,updated_at TEXT,revision INTEGER,importance REAL,
                parent_id TEXT,data TEXT,deleted INTEGER DEFAULT 0);
            CREATE TABLE revisions(record_id TEXT,revision INTEGER,changed_at TEXT,action TEXT,reason TEXT,
                data TEXT,PRIMARY KEY(record_id,revision));
            CREATE TABLE evidence(record_id TEXT,source_id TEXT,PRIMARY KEY(record_id,source_id));
            CREATE TABLE tombstones(key TEXT PRIMARY KEY,deleted_at TEXT);
            CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT,unique_key TEXT UNIQUE,payload TEXT,state TEXT,
                attempts INTEGER DEFAULT 0,max_attempts INTEGER DEFAULT 5,available REAL,lease_until REAL,
                owner TEXT,fence INTEGER DEFAULT 0,error TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE outbox(id TEXT PRIMARY KEY,schedule_id TEXT,state TEXT,attempts INTEGER DEFAULT 0,
                available REAL,lease_until REAL,data TEXT,UNIQUE(schedule_id,id));
            CREATE TABLE vector_indexes(id TEXT PRIMARY KEY,data TEXT);
        """)
        conn.execute("INSERT INTO meta VALUES('schema_version',1)")
        conn.execute("INSERT INTO meta VALUES('generation',2)")
        conn.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)", (
            source_id, "legacy", "item", "7", Scope().key(), "old-session", STAMP, STAMP,
            key, key, dumps(source_data), "complete", "not_requested",
        ))
        conn.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)", (
            record_id, "episode", Scope().key(), "active", STAMP, None, STAMP, STAMP,
            2, .5, None, dumps(second),
        ))
        conn.executemany("INSERT INTO revisions VALUES(?,?,?,?,?,?)", [
            (record_id, 1, STAMP, "create", "", dumps(base)),
            (record_id, 2, STAMP, "correct", "user correction", dumps(second)),
        ])
        conn.execute("INSERT INTO evidence VALUES(?,?)", (record_id, source_id))
        conn.execute("INSERT INTO tombstones VALUES(?,?)", ("mem_deleted", STAMP))
        conn.execute("INSERT INTO tombstones VALUES(?,?)", ("src_deleted", STAMP))
        conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "job_original", "summary", "old-job", "{}", "running", 1, 5, 0, 9999999999,
            "old-worker", 1, None, STAMP, STAMP,
        ))
        conn.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?)", (
            "delivery_original", "schedule_original", "sending", 1, 0, 9999999999,
            dumps({"message": "may already have been sent"}),
        ))
        conn.execute("INSERT INTO vector_indexes VALUES(?,?)", (
            "vec_original", dumps({"id": "vec_original", "dimensions": 3, "state": "ready"}),
        ))
    (root / "host-spool").mkdir()
    (root / "host-spool" / "pending.json").write_text('{"event":"end"}')
    return source_id, record_id


def _event(event_id: str, *, status: str = "open", parent: str | None = None) -> bytes:
    return (
        "---\n"
        f"id: {event_id}\n"
        f"parent: {parent or 'null'}\n"
        "kind: build\n"
        f"status: {status}\n"
        "intent: Preserve this work\n"
        "anchors:\n  files:\n  - src/example.py\n"
        "outcome: null\nlesson: null\n"
        "---\n\nThe original action text.\n"
    ).encode()


def test_sqlite_migration_keeps_identity_history_deletion_and_unfinished_work(tmp_path):
    source = tmp_path / "old"
    sid, rid = _old_sqlite(source)
    before = (source / "memory.sqlite3").read_bytes()
    target = tmp_path / "new"
    report = migrate(source, target)
    assert report["format"] == "sqlite-v1"
    assert report["running_jobs_requeued"] == 1
    assert report["sending_deliveries_uncertain"] == 1
    assert (source / "memory.sqlite3").read_bytes() == before
    assert (target / "host-spool" / "pending.json").exists()
    migrated = Engine(target)
    assert migrated.source(sid)["id"] == sid
    assert migrated.get(rid)["content"] == "corrected version"
    assert [r["data"]["content"] for r in migrated.history(rid)] == ["corrected version", "first version"]
    assert [r["action"] for r in migrated.history(rid)] == ["correct", "create"]
    with pytest.raises(Missing):
        migrated.get("mem_deleted")
    with migrated.db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT key FROM tombstones")} == {"mem_deleted", "src_deleted"}
        assert conn.execute("SELECT state FROM jobs WHERE id='job_original'").fetchone()[0] == "pending"
        assert conn.execute("SELECT state FROM outbox WHERE id='delivery_original'").fetchone()[0] == "uncertain"
        assert conn.execute("SELECT COUNT(*) FROM search WHERE id=?", (rid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'").fetchone()[0] == 1
        assert json.loads(conn.execute("SELECT data FROM vector_indexes").fetchone()[0])["state"] == "pending"
    assert (target / "blobs" / digest(b"original attachment\n")).read_bytes() == b"original attachment\n"


def test_sqlite_migration_rejects_missing_attachment_without_partial_target(tmp_path):
    source = tmp_path / "old"
    _old_sqlite(source)
    next((source / "blobs").iterdir()).unlink()
    target = tmp_path / "empty"
    target.mkdir()
    with pytest.raises(ValueError, match="Missing legacy attachment"):
        migrate(source, target)
    assert list(target.iterdir()) == []


def test_sqlite_migration_reads_wal_snapshot_without_modifying_source(tmp_path):
    source = tmp_path / "old"
    _old_sqlite(source)
    database = source / "memory.sqlite3"
    with sqlite3.connect(database) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("INSERT INTO tombstones VALUES(?,?)", ("wal_deletion", STAMP))
        writer.commit()
        before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        target = tmp_path / "new"
        migrate(database, target)
        after = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        assert after == before
    with Engine(target).db.connect() as conn:
        assert conn.execute("SELECT 1 FROM tombstones WHERE key='wal_deletion'").fetchone()


def test_file_store_migration_preserves_raw_files_archives_and_ids(tmp_path):
    source = tmp_path / ".memory"
    (source / "events").mkdir(parents=True)
    (source / "archive").mkdir()
    (source / "index").mkdir()
    active = _event("event-open", parent="event-frozen")
    (source / "events" / "event-open.md").write_bytes(active)
    (source / "index" / "archive-index.md").write_text("# Archive index\n\nevent-frozen | 2026-Q1 | Old work\n")
    archived = _event("event-frozen", status="done")
    with tarfile.open(source / "archive" / "epoch-2026-Q1.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("events/event-frozen.md")
        info.size = len(archived)
        archive.addfile(info, io.BytesIO(archived))
    target = tmp_path / "new"
    report = migrate(source, target, Scope(project="old-project"))
    assert report["format"] == "file-v0"
    assert report["records"] == 2
    assert report["snapshots"] == 3
    engine = Engine(target)
    assert engine.get("event-open")["status"] == "active"
    assert engine.get("event-open")["parent_id"] == "event-frozen"
    assert engine.get("event-frozen")["status"] == "archived"
    assert engine.get("event-open")["attributes"]["anchors"]["files"] == ["src/example.py"]
    with engine.db.connect() as conn:
        row = conn.execute("SELECT blob FROM sources WHERE source_key='events/event-open.md'").fetchone()
    assert (target / "blobs" / row[0]).read_bytes() == active
    assert (source / "events" / "event-open.md").read_bytes() == active
    with pytest.raises(Conflict, match="empty isolated target"):
        migrate(source, target)


def test_migration_cli_requires_explicit_target():
    from eventmem.core.cli import parser

    with pytest.raises(SystemExit):
        parser().parse_args(["migrate", "/old/.memory"])
