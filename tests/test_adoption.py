"""观测与评估埋点（SPEC §3.13）：采纳判定（浮现→是否在窗口内被 Edit）、
处理后文件改名 .done、transcript 定位（含 dsh feed 命名）、证据累加进 salience.json。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eventmem.consolidate import deep
from eventmem.index import Budget, load_salience
from eventmem.schema import Anchors
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)


def _low_threshold(paths) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")


def _write_surfaced(paths, session: str, event_id: str, ts: str) -> Path:
    path = paths.log_dir / f"surfaced-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": ts, "event_id": event_id, "cue": "src/a.py", "cue_kind": "file", "chars": 5}) + "\n",
        encoding="utf-8",
    )
    return path


def _tool_use_record(timestamp: str | None, name: str, file_path: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": name, "input": {"file_path": file_path}}],
        }
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _real_file(paths, rel: str) -> str:
    abs_path = paths.project_dir / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("x", encoding="utf-8")
    return str(abs_path)


# ==================================================================== hit / ignored 基本判定


def test_adoption_is_a_hit_when_edit_touches_the_anchor_file_after_surfacing(
    store: Store, paths, event_factory
) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    file_path = _real_file(paths, "src/a.py")
    _write_transcript(
        paths.log_dir / "sess1.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", file_path)]
    )

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert evidence["hits"] == 1
    assert evidence["ignored"] == 0


def test_adoption_is_ignored_when_no_matching_edit_occurs(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    other_file = _real_file(paths, "src/unrelated.py")
    _write_transcript(
        paths.log_dir / "sess1.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", other_file)]
    )

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert evidence["hits"] == 0
    assert evidence["ignored"] == 1


def test_adoption_ignores_read_only_tool_calls_on_the_matching_file(store: Store, paths, event_factory) -> None:
    """只有写类工具（Edit/Write/MultiEdit/NotebookEdit）算数；Read 命中同一文件不算采纳。"""
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    file_path = _real_file(paths, "src/a.py")
    _write_transcript(
        paths.log_dir / "sess1.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Read", file_path)]
    )

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert evidence["hits"] == 0
    assert evidence["ignored"] == 1


# ==================================================================== 10 次工具调用窗口边界


def test_adoption_hit_within_the_ten_tool_window(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    other = _real_file(paths, "src/filler.py")
    target = _real_file(paths, "src/a.py")
    records = [_tool_use_record(f"2026-08-25T10:00:{i:02d}", "Read", other) for i in range(1, 10)]
    records.append(_tool_use_record("2026-08-25T10:00:10", "Edit", target))  # 窗口内第 10 次调用
    _write_transcript(paths.log_dir / "sess1.jsonl", records)

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (1, 0)


def test_adoption_ignored_beyond_the_ten_tool_window(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    other = _real_file(paths, "src/filler.py")
    target = _real_file(paths, "src/a.py")
    records = [_tool_use_record(f"2026-08-25T10:00:{i:02d}", "Read", other) for i in range(1, 11)]
    records.append(_tool_use_record("2026-08-25T10:00:11", "Edit", target))  # 第 11 次，超出窗口
    _write_transcript(paths.log_dir / "sess1.jsonl", records)

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (0, 1)


def test_adoption_falls_back_to_full_session_scan_when_timestamps_are_missing(
    store: Store, paths, event_factory
) -> None:
    """埋点与 transcript 的时间戳对不齐（这里是 transcript 完全没有时间戳）时，
    退化为全会话扫描，而不是把窗口外的浮现一律判成 ignored。"""
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    other = _real_file(paths, "src/filler.py")
    target = _real_file(paths, "src/a.py")
    records = [_tool_use_record(None, "Read", other) for _ in range(14)]  # 无时间戳，远超 10 次窗口
    records.append(_tool_use_record(None, "Edit", target))
    _write_transcript(paths.log_dir / "sess1.jsonl", records)

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (1, 0)


# ==================================================================== transcript 定位


def test_adoption_neither_hit_nor_ignored_when_transcript_cannot_be_found(
    store: Store, paths, event_factory
) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    surfaced_path = _write_surfaced(paths, "sess-nowhere-to-be-found-xyz123", e1, "2026-08-25T10:00:00")

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (0, 0)
    # 无论是否找到 transcript，处理过的埋点文件都会被改名，避免下次深整理重复计数
    assert not surfaced_path.exists()
    assert surfaced_path.with_name(surfaced_path.name + ".done").exists()


def test_adoption_finds_dsh_feed_named_transcript(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    _write_surfaced(paths, "sess-dsh", e1, "2026-08-25T10:00:00")
    target = _real_file(paths, "src/a.py")
    _write_transcript(
        paths.log_dir / "dsh-feed-sess-dsh.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", target)]
    )

    deep(store, paths, Budget(), None, NOW)

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (1, 0)


# ==================================================================== 处理后改名 .done


def test_surfaced_file_is_renamed_to_done_after_processing(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    surfaced_path = _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    target = _real_file(paths, "src/a.py")
    _write_transcript(paths.log_dir / "sess1.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", target)])

    deep(store, paths, Budget(), None, NOW)

    assert not surfaced_path.exists()
    done_path = surfaced_path.with_name(surfaced_path.name + ".done")
    assert done_path.exists()

    # 重跑一次深整理：已处理过的 .done 文件不会被重复扫描，证据不会翻倍
    store.append(event_factory(status="done", outcome="无关事件", intent="无关意图"))
    deep(store, paths, Budget(), None, NOW)
    evidence = load_salience(paths)[e1]["evidence"]
    assert evidence["hits"] == 1  # 仍是 1，不是 2


# ==================================================================== 证据累加进 salience.json


def test_adoption_evidence_accumulates_across_multiple_deep_runs(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))
    target = _real_file(paths, "src/a.py")

    _write_surfaced(paths, "sessA", e1, "2026-08-25T10:00:00")
    _write_transcript(paths.log_dir / "sessA.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", target)])
    deep(store, paths, Budget(), None, NOW)
    assert load_salience(paths)[e1]["evidence"]["hits"] == 1

    store.append(event_factory(status="done", outcome="无关事件", intent="无关意图"))  # 推高脏量
    _write_surfaced(paths, "sessB", e1, "2026-08-25T11:00:00")
    _write_transcript(paths.log_dir / "sessB.jsonl", [_tool_use_record("2026-08-25T11:00:05", "Edit", target)])
    deep(store, paths, Budget(), None, NOW)

    assert load_salience(paths)[e1]["evidence"]["hits"] == 2  # 累加而非覆盖


def test_adoption_pass_is_safe_with_no_surfaced_files_at_all(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))

    deep(store, paths, Budget(), None, NOW)  # 不应抛异常

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (0, 0)


def test_events_without_file_anchors_are_excluded_from_adoption_judging(store: Store, paths, event_factory) -> None:
    _low_threshold(paths)
    e1 = store.append(event_factory(kind="decision", status="done", outcome="选定方案", anchors=Anchors()))
    _write_surfaced(paths, "sess1", e1, "2026-08-25T10:00:00")
    target = _real_file(paths, "src/unrelated.py")
    _write_transcript(paths.log_dir / "sess1.jsonl", [_tool_use_record("2026-08-25T10:00:05", "Edit", target)])

    deep(store, paths, Budget(), None, NOW)  # 不应抛异常

    evidence = load_salience(paths)[e1]["evidence"]
    assert (evidence["hits"], evidence["ignored"]) == (0, 0)
