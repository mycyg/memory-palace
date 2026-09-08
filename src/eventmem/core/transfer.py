from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .db import Conflict, digest, dumps
from .engine import Engine
from .models import RecordInput, Scope, SourceInput


def migrate(legacy: Path, target: Path, scope: Scope):
    from eventmem.schema import from_markdown

    legacy, target = legacy.expanduser().resolve(), target.expanduser().resolve()
    if legacy == target or legacy in target.parents or target in legacy.parents:
        raise ValueError("Migration target must be separate from the legacy directory")
    if target.exists() and any(target.iterdir()):
        raise Conflict("Migration requires an empty isolated target")
    if not legacy.is_dir():
        raise ValueError("Legacy directory does not exist")
    engine = Engine(target)
    events = {}
    unknown = []
    snapshots = []
    for path in sorted(legacy.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(legacy).as_posix()
        raw = path.read_bytes()
        sid = engine.receive(
            SourceInput(
                namespace="legacy-snapshot",
                key=relative,
                title=relative,
                scope=scope,
                media_type="application/octet-stream",
                authority="document",
                metadata={"legacy_path": relative},
            ),
            raw,
        )["id"]
        snapshots.append(sid)
        # Snapshots must be preserved without treating every internal index as a
        # knowledge document or scheduling a parser for log/config files.
        with engine.db.connect(write=True) as conn:
            conn.execute("UPDATE sources SET mechanical='complete' WHERE id=?", (sid,))
            conn.execute(
                "UPDATE jobs SET state='canceled' WHERE kind='parse' AND json_extract(payload,'$.source_id')=?",
                (sid,),
            )
        candidates = (
            [(relative, raw)] if path.suffix == ".md" and "events" in path.parts else []
        )
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if (
                        not member.isfile()
                        or member.size > 10_000_000
                        or not member.name.endswith(".md")
                    ):
                        continue
                    handle = archive.extractfile(member)
                    if handle:
                        candidates.append(
                            (relative + "::" + member.name, handle.read())
                        )
        for location, content in candidates:
            try:
                event = from_markdown(content.decode())
                events.setdefault(event.id, (event, sid, location))
            except Exception:
                unknown.append(location)
    status_map = {"superseded": "superseded", "abandoned": "archived"}
    frozen_ids = set()
    from eventmem.index import load_archive_index
    from eventmem.paths import MemoryPaths

    frozen_ids.update(load_archive_index(MemoryPaths.for_project(legacy.parent)))
    # Preserve authoritative legacy metadata as snapshots even if its schema is
    # unknown; known archive indexes additionally restore current archive state.
    for filename in (
        legacy / "index" / "archive-index.json",
        legacy / "archive-index.json",
    ):
        if filename.exists():
            try:
                archive = json.loads(filename.read_text())
                frozen_ids.update(
                    archive if isinstance(archive, dict) else [r["id"] for r in archive]
                )
            except (ValueError, KeyError):
                unknown.append(str(filename.relative_to(legacy)))
    for event, sid, location in events.values():
        attributes = {
            "legacy_kind": event.kind,
            "legacy_status": event.status,
            "intent": event.intent,
            "outcome": event.outcome,
            "lesson": event.lesson,
            "anchors": event.anchors.__dict__,
            "legacy_parent": event.parent,
            "superseded_by": event.superseded_by,
            "source_completeness": "external_dialog_pointers_unverified",
            "legacy_location": location,
        }
        record = RecordInput(
            id=event.id,
            kind="episode",
            title=event.intent,
            content="\n\n".join(
                s for s in [event.intent, event.body, event.outcome, event.lesson] if s
            ),
            scope=scope,
            source_ids=[sid],
            status="archived"
            if event.id in frozen_ids or "::" in location
            else status_map.get(event.status, "active"),
            confirmation="observed",
            attributes=attributes,
            locator={"legacy_path": location},
        )
        engine.add_record(record, "migration:" + event.id)
    with engine.db.connect(write=True) as conn:
        for event, _, _ in events.values():
            if event.parent and event.parent in events:
                data = engine._get(conn, event.id)
                data["parent_id"] = event.parent
                conn.execute(
                    "UPDATE records SET parent_id=?,data=? WHERE id=?",
                    (event.parent, dumps(data), event.id),
                )
                conn.execute(
                    "UPDATE revisions SET data=? WHERE record_id=? AND revision=1",
                    (dumps(data), event.id),
                )
            if event.superseded_by and event.superseded_by in events:
                engine._relation(
                    conn,
                    event.id,
                    "superseded_by",
                    event.superseded_by,
                    {"basis": "legacy"},
                )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    report = {
        "legacy_path": str(legacy),
        "target_path": str(target),
        "records": len(events),
        "snapshots": len(snapshots),
        "unparsed_metadata": unknown,
        "integrity": integrity,
        "original_unchanged": True,
        "source_completeness": "Original event files preserved; external dialog references require separate import.",
        "restore": "Point the host back to its original .memory directory to resume the legacy adapter.",
    }
    (target / "migration-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    return report


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
