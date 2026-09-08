"""Reproducible adapters for retrieval replay; no inferred competitor scores."""

from __future__ import annotations
import io, json, os, subprocess, sys, tarfile, tempfile
from pathlib import Path
from typing import Protocol

LEGACY_REVISION = "e66710c06c4c5afae1cdcba2ed967897020c40eb"


class RecallAdapter(Protocol):
    def recall(
        self, *, query: str, records: list[dict], budget: int, cutoff: str
    ) -> list[str]: ...


class LegacyAdapter:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="memorypalace-legacy-baseline-"
        )
        self.root = Path(self.directory.name)
        repo = Path(__file__).resolve().parents[3]
        archive = subprocess.run(
            ["git", "archive", LEGACY_REVISION, "src/eventmem"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as file:
            file.extractall(self.root, filter="data")

    def recall(self, *, query, records, budget, cutoff):
        code = """
import json,sys,tempfile
from datetime import datetime
sys.path.insert(0,sys.argv[1])
from eventmem.paths import MemoryPaths
from eventmem.store import Store
from eventmem.schema import make_event
from eventmem.index import rebuild_all,Budget
from eventmem.recall import search
payload=json.load(sys.stdin)
with tempfile.TemporaryDirectory() as root:
    paths=MemoryPaths.for_project(root);paths.ensure();store=Store(paths);mapping={}
    for i,r in enumerate(payload['records']):
        eid='2026-08-25_'+str(100000+i)
        status='done' if r['status']=='active' else 'superseded' if r['status']=='superseded' else 'abandoned'
        store.append(make_event(eid,'build',status,r['content'],outcome=r['content'],body=r['content']))
        mapping[eid]=r['id']
    rebuild_all(store,paths,Budget(),datetime(2026,9,8))
    print(json.dumps([mapping[h.event_id] for h in search(payload['query'],store,paths,top=20)]))
"""
        env = os.environ.copy()
        env["EVENTMEM_GLOBAL_DIR"] = str(self.root / "global")
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.root / "src")],
            input=json.dumps({"records": records, "query": query}),
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        return json.loads(result.stdout)

    def close(self):
        self.directory.cleanup()
