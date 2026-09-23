from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

import yaml

from .db import Conflict, digest, dumps
from .engine import Engine
from .legacy_reader import archived_ids, event_from_markdown
from .models import RecordInput, Scope, SourceInput


def migrate(legacy: Path, target: Path, scope: Scope | None = None):
    """Copy a v1 SQLite root or historical `.memory` directory into an empty root.

    The source is opened read-only. All writes happen in a sibling staging directory;
    a failed validation leaves the requested target empty and usable for a retry.
    """
    legacy, target = Path(legacy).expanduser(), Path(target).expanduser()
    if legacy.is_symlink() or target.is_symlink():
        raise ValueError("Migration does not follow a source or target symlink")
    legacy, target = legacy.resolve(), target.resolve()
    source_root = legacy.parent if legacy.is_file() else legacy
    if not source_root.is_dir():
        raise ValueError("Legacy store does not exist")
    if source_root == target or source_root in target.parents or target in source_root.parents:
        raise ValueError("Migration target must be separate from the legacy directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise Conflict("Migration requires an empty isolated target")
    sqlite_path = legacy if legacy.is_file() else legacy / "memory.sqlite3"
    file_store = not sqlite_path.is_file()
    if file_store and not (legacy / "events").is_dir() and not (legacy / "archive").is_dir():
        raise ValueError("Legacy path is neither a SQLite store nor a file store")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".memorypalace-migrate-", dir=target.parent))
    try:
        report = (
            _migrate_files(source_root, stage, scope or Scope())
            if file_store
            else _migrate_sqlite(sqlite_path, source_root, stage)
        )
        report.update(legacy_path=str(legacy), target_path=str(target), original_unchanged=True)
        (stage / "migration-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if target.exists():
            if any(target.iterdir()):
                raise Conflict("Migration target became nonempty")
            target.rmdir()
        stage.rename(target)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _migrate_sqlite(database: Path, source_root: Path, stage: Path) -> dict:
    # Opening a WAL database with mode=ro can still modify its -shm file. Copy
    # the authoritative DB and WAL bytes first, then let SQLite touch only stage.
    source_hashes: dict[Path, str] = {}
    for suffix in ("", "-wal"):
        original = Path(str(database) + suffix)
        if suffix and not original.exists():
            continue
        if original.is_symlink() or not original.is_file():
            raise ValueError("Legacy SQLite store contains an invalid database file")
        before = _file_digest(original)
        source_hashes[original] = before
        copied = stage / ("memory.sqlite3" + suffix)
        shutil.copyfile(original, copied)
        if _file_digest(original) != before or _file_digest(copied) != before:
            raise Conflict("Legacy SQLite store changed during snapshot")
    if any(_file_digest(path) != expected for path, expected in source_hashes.items()):
        raise Conflict("Legacy SQLite store changed during snapshot")
    with sqlite3.connect(stage / "memory.sqlite3") as snapshot:
        tables = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"meta", "sources", "records", "revisions", "tombstones", "jobs"}
        if not required <= tables:
            raise ValueError("Unrecognized MemoryPalace SQLite schema")
        version = snapshot.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != 1:
            raise ValueError("Unsupported MemoryPalace SQLite schema version")
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Legacy SQLite database is corrupt")
    engine = Engine(stage)
    with engine.db.connect() as conn:
        blobs = {row[0] for row in conn.execute("SELECT blob FROM sources WHERE blob IS NOT NULL AND deleted=0")}
        for table in ("records", "revisions"):
            column = "data"
            for row in conn.execute(f"SELECT {column} FROM {table}"):
                locator = json.loads(row[0]).get("locator") or {}
                if locator.get("blob"):
                    blobs.add(locator["blob"])
    for key in blobs:
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Legacy attachment has an invalid digest")
        source_blob = source_root / "blobs" / key
        if not source_blob.is_file() or source_blob.is_symlink():
            raise ValueError(f"Missing legacy attachment {key}")
        content = source_blob.read_bytes()
        if digest(content) != key:
            raise ValueError(f"Legacy attachment checksum mismatch {key}")
        output = engine.db.blobs / key
        output.write_bytes(content)
        output.chmod(0o600)
    spool = source_root / "host-spool"
    if spool.is_symlink():
        raise ValueError("Legacy host spool is a symlink")
    if spool.is_dir():
        for path in spool.rglob("*"):
            if path.is_symlink():
                raise ValueError("Legacy host spool contains a symlink")
        shutil.copytree(spool, stage / "host-spool")
    with engine.db.connect(write=True) as conn:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("sources", "records", "revisions", "tombstones", "jobs")
        }
        running = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='running'").fetchone()[0]
        sending = conn.execute("SELECT COUNT(*) FROM outbox WHERE state='sending'").fetchone()[0]
        conn.execute("UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL,available=?,fence=fence+1 WHERE state='running'", (time.time(),))
        conn.execute("UPDATE outbox SET state='uncertain',lease_until=NULL WHERE state='sending'")
        conn.execute("DELETE FROM search")
        conn.execute("DELETE FROM prefetch")
        conn.execute("DELETE FROM dirty")
        conn.execute("UPDATE vector_indexes SET data=json_set(data,'$.state','pending','$.indexed_at',NULL)")
        for row in conn.execute("SELECT data FROM records WHERE deleted=0"):
            record = json.loads(row[0])
            engine._index_text(conn, record)
            engine._dirty(conn, record)
        # The LanceDB directory is derived and intentionally excluded. Completed
        # embedding jobs must be runnable again against the new empty index.
        conn.execute("UPDATE jobs SET state='pending',available=?,owner=NULL,lease_until=NULL,attempts=0 WHERE kind IN ('embed','visual_embed','build_vectors','purge_vectors') AND state='complete'", (time.time(),))
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Migrated SQLite database failed integrity check")
    return {
        "format": "sqlite-v1",
        **counts,
        "attachments": len(blobs),
        "running_jobs_requeued": running,
        "sending_deliveries_uncertain": sending,
        "derived_indexes": "lexical rebuilt; vector embeddings queued",
        "integrity": "ok",
    }


def _file_digest(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def _migrate_files(legacy: Path, stage: Path, scope: Scope) -> dict:
    engine = Engine(stage)
    events: dict[str, tuple] = {}
    unparsed: list[str] = []
    snapshots = 0
    for path in sorted(legacy.rglob("*")):
        if path.is_symlink():
            raise ValueError("Legacy file store contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(legacy).as_posix()
        raw = path.read_bytes()
        sid = engine.receive(
            SourceInput(
                namespace="legacy-file", key=relative if len(relative) <= 500 else digest(relative),
                title=relative[:1000], scope=scope, media_type="application/octet-stream",
                authority="document", metadata={"legacy_path": relative},
            ), raw,
        )["id"]
        snapshots += 1
        with engine.db.connect(write=True) as conn:
            conn.execute("UPDATE sources SET mechanical='complete' WHERE id=?", (sid,))
            conn.execute("UPDATE jobs SET state='canceled' WHERE kind='parse' AND json_extract(payload,'$.source_id')=?", (sid,))
        candidates = [(relative, raw)] if path.parent == legacy / "events" and path.suffix == ".md" else []
        if path.name.endswith((".tar", ".tar.gz", ".tgz")):
            try:
                with tarfile.open(path) as archive:
                    for member in archive:
                        if not member.isfile() or not member.name.endswith(".md"):
                            continue
                        if member.size > 10_000_000:
                            unparsed.append(relative + "::" + member.name)
                            continue
                        handle = archive.extractfile(member)
                        if handle:
                            candidates.append((relative + "::" + member.name, handle.read()))
            except (tarfile.TarError, OSError):
                unparsed.append(relative)
        for location, content in candidates:
            try:
                event = event_from_markdown(content)
                # The live event file is authoritative when an archive also contains
                # an older version. Every raw copy remains in its source snapshot.
                if event.id not in events or "::" not in location:
                    events[event.id] = (event, sid, location)
            except (ValueError, TypeError, UnicodeError, yaml.YAMLError):
                unparsed.append(location)
    frozen = archived_ids(legacy)
    for event, sid, location in events.values():
        attributes = {
            "legacy_kind": event.kind, "legacy_status": event.status,
            "intent": event.intent, "outcome": event.outcome,
            "lesson": event.lesson, "anchors": event.anchors,
            "legacy_parent": event.parent, "superseded_by": event.superseded_by,
            "salience_prior": event.salience_prior, "salience_reason": event.salience_reason,
            "prospective": event.prospective,
            "source_completeness": "external_dialog_pointers_unverified",
            "legacy_location": location,
        }
        status = "archived" if event.id in frozen or "::" in location or event.status == "abandoned" else "superseded" if event.status == "superseded" else "active"
        engine.add_record(
            RecordInput(
                id=event.id, kind="episode", title=event.intent[:1000],
                content="\n\n".join(s for s in (event.intent, event.body, event.outcome, event.lesson) if s),
                scope=scope, source_ids=[sid], status=status, confirmation="observed",
                attributes=attributes, locator={"legacy_path": location},
            ),
            "migration:" + event.id,
        )
    with engine.db.connect(write=True) as conn:
        for event, _, _ in events.values():
            if event.parent in events:
                data = engine._get(conn, event.id)
                data["parent_id"] = event.parent
                conn.execute("UPDATE records SET parent_id=?,data=? WHERE id=?", (event.parent, dumps(data), event.id))
                conn.execute("UPDATE revisions SET data=? WHERE record_id=? AND revision=1", (dumps(data), event.id))
                engine._relation(conn, event.id, "part_of", event.parent, {"basis": "legacy"})
            if event.superseded_by in events:
                engine._relation(conn, event.id, "superseded_by", event.superseded_by, {"basis": "legacy"})
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Migrated file store failed integrity check")
    return {
        "format": "file-v0", "records": len(events), "snapshots": snapshots,
        "unparsed_events": unparsed,
        "source_completeness": "Raw files and archive packages preserved; external dialog pointers remain unverified.",
        "derived_indexes": "rebuilt from records; embeddings queued",
        "integrity": "ok",
    }


def backup(engine, output: Path):
    output = output.expanduser().resolve()
    if output.exists():
        raise Conflict("Backup output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memorypalace-backup-") as temp:
        snapshot = Path(temp)
        with engine.db.connect(write=True):
            source = sqlite3.connect(engine.db.path)
            target = sqlite3.connect(snapshot / "memory.sqlite3")
            source.backup(target)
            source.close()
            target.close()
            shutil.copytree(engine.db.blobs, snapshot / "blobs")
        manifest = {
            p.relative_to(snapshot).as_posix(): digest(p.read_bytes())
            for p in snapshot.rglob("*")
            if p.is_file()
        }
        (snapshot / "manifest.json").write_text(
            dumps({"format": "memorypalace-1", "files": manifest})
        )
        with tarfile.open(output, "w:gz") as archive:
            for path in snapshot.iterdir():
                archive.add(path, arcname=path.name, recursive=True)
    output.chmod(0o600)
    return {
        "path": str(output),
        "files": len(manifest),
        "indexes": "Rebuildable and excluded",
        "credentials": "Excluded",
    }


def restore(archive_path: Path, target: Path):
    target = target.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise Conflict("Restore requires an empty isolated target")
    with tempfile.TemporaryDirectory(prefix="memorypalace-restore-") as temp:
        root = Path(temp)
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or member.issym()
                    or member.islnk()
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError("Unsafe archive entry")
                if member.isfile():
                    output = root / path
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, output.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["format"] != "memorypalace-1":
            raise ValueError("Unknown backup format")
        for name, expected in manifest["files"].items():
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not (
                    name == "memory.sqlite3"
                    or (
                        len(relative.parts) == 2
                        and relative.parts[0] == "blobs"
                        and len(relative.parts[1]) == 64
                        and all(c in "0123456789abcdef" for c in relative.parts[1])
                    )
                )
            ):
                raise ValueError("Unexpected backup content")
            path = (root / name).resolve()
            if (
                not path.is_relative_to(root.resolve())
                or digest(path.read_bytes()) != expected
            ):
                raise ValueError("Backup checksum mismatch")
        actual = {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        if actual != set(manifest["files"]):
            raise ValueError("Backup contains unverified files")
        with sqlite3.connect(root / "memory.sqlite3") as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database is corrupt")
        shutil.copytree(root, target, dirs_exist_ok=True)
    engine = Engine(target)
    with engine.db.connect(write=True) as conn:
        conn.execute(
            "UPDATE jobs SET state='pending',owner=NULL,lease_until=NULL WHERE state='running'"
        )
        conn.execute("UPDATE outbox SET state='uncertain' WHERE state='sending'")
        conn.execute(
            "UPDATE vector_indexes SET data=json_set(data,'$.state','pending')"
        )
    engine.enqueue("rebuild", {}, "restore-rebuild")
    return {
        "path": str(target),
        "status": "restored",
        "rebuild": "queued",
        "contacts": "Prior sending state requires reconciliation",
    }


def export_records(engine, output: Path):
    if output.exists():
        raise Conflict("Export output already exists")
    with engine.db.connect() as conn, output.open("x", encoding="utf-8") as f:
        for row in conn.execute("SELECT data FROM records WHERE deleted=0 ORDER BY id"):
            f.write(row[0] + "\n")
    output.chmod(0o600)
    return {"path": str(output), "format": "jsonl"}
