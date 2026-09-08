from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import shutil

from eventmem.extract import extract_events
from eventmem.index import (
    ArchiveRow,
    Budget,
    load_archive_index,
    rebuild_all,
    write_archive_index,
)
from eventmem.llm import LLMError
from eventmem.recall import surface
from eventmem.schema import Anchors


def test_legacy_parallel_same_id_preserves_every_event(store, event_factory):
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(
            pool.map(
                lambda i: store.append(
                    event_factory(id="2026-09-08_000000", intent=f"Intent {i}")
                ),
                range(50),
            )
        )
    assert len(set(ids)) == 50
    assert len(store.all_ids()) == 50


def test_legacy_failed_model_stage_is_retried(store, paths, tmp_path, fake_llm):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"Use the stable port allocator"}}\n'
    )
    fake_llm.queue(LLMError("timeout"))
    extract_events(transcript, store, fake_llm, "retry", datetime.now())
    calls = len(fake_llm.calls)
    fake_llm.queue({"events": []})
    extract_events(transcript, store, fake_llm, "retry", datetime.now())
    assert len(fake_llm.calls) == calls + 1


def test_legacy_superseded_anchor_not_injected(store, paths, event_factory):
    rid = store.append(
        event_factory(status="done", outcome="old", anchors=Anchors(files=["same.py"]))
    )
    newer = store.append(
        event_factory(status="done", outcome="new", anchors=Anchors(files=["same.py"]))
    )
    store.mark_superseded(rid, newer)
    rebuild_all(store, paths, Budget(), datetime.now())
    assert all(
        hit.event_id != rid
        for hit in surface("same.py", "file", store, paths, Budget(), set())
    )


def test_legacy_archive_state_survives_index_deletion(store, paths, event_factory):
    rid = store.append(event_factory(status="done", outcome="cold"))
    write_archive_index(paths, {rid: ArchiveRow(id=rid, epoch="2026Q3", intent="cold")})
    shutil.rmtree(paths.index_dir)
    rebuild_all(store, paths, Budget(), datetime.now())
    assert rid in load_archive_index(paths)
