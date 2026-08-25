"""分级遗忘（SPEC §3.19）：冷却、冻结、解冻、清除。

贯穿全文件的纪律前提：冷却只写派生层；events/ 散文件的删除只发生在「打包 → 解包
校验通过 → 纪元摘要落盘」之后；L0 内容永不有损（包内字节与冻结前逐字节相同）。
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta

from eventmem import consolidate
from eventmem.cli import main
from eventmem.consolidate import EPOCH_SYSTEM, archive_pass, deep
from eventmem.index import (
    Budget,
    epoch_of,
    load_archive_index,
    rebuild_all,
    salience_file,
)
from eventmem.paths import MemoryPaths, atomic_write
from eventmem.recall import search_archive
from eventmem.schema import Anchors, Event
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)
COLD_ID = "2026-01-05_101500"  # 年龄 > 90 天，< 365 天
FROZEN_ID = "2024-05-06_101500"  # 年龄 > 365 天
NO_STATE: dict[str, object] = {"runs": 0, "events": {}}


def _write_salience(paths: MemoryPaths, events: list[Event], **evidence: int) -> None:
    """写一份让事件全部够冷却门槛的 salience.json（score 0.05、零证据）。"""
    payload = {
        event.id: {
            "score": 0.05,
            "prior": "low",
            "evidence": {
                "refs": evidence.get("refs", 0),
                "hits": evidence.get("hits", 0),
                "ignored": 0,
                "superseded_trigger": False,
            },
            "updated": NOW.isoformat(timespec="seconds"),
        }
        for event in events
    }
    atomic_write(salience_file(paths), json.dumps(payload, ensure_ascii=False))


def _cool(paths: MemoryPaths, store: Store, state: dict | None = None) -> dict[str, int]:
    """跑一遍归档 pass（无 LLM），返回计数。"""
    events = list(store.iter_events())
    _write_salience(paths, events)
    return archive_pass(paths, events, state or NO_STATE, None, NOW)


# ==================================================================== 冷却判据


def test_old_closed_event_with_no_evidence_is_cooled(store: Store, paths, event_factory) -> None:
    event_id = store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))

    result = _cool(paths, store)

    rows = load_archive_index(paths)
    assert result["cooled"] == 1
    assert rows[event_id].epoch == "2026-Q1"
    assert rows[event_id].intent == "示例意图"
    assert paths.event_file(event_id).is_file()  # 冷却不动 L0


def test_recent_event_is_not_cooled(store: Store, paths, event_factory) -> None:
    recent = (NOW - timedelta(days=10)).strftime("%Y-%m-%d_%H%M%S")
    store.append(event_factory(id=recent, status="done", outcome="完成"))

    assert _cool(paths, store)["cooled"] == 0
    assert not load_archive_index(paths)


def test_event_with_reference_or_hit_evidence_is_not_cooled(store: Store, paths, event_factory) -> None:
    """判据要全部满足：refs 或 hits 非零就留在活跃层。"""
    store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))
    events = list(store.iter_events())

    _write_salience(paths, events, refs=1)
    assert archive_pass(paths, events, NO_STATE, None, NOW)["cooled"] == 0

    _write_salience(paths, events, hits=1)
    assert archive_pass(paths, events, NO_STATE, None, NOW)["cooled"] == 0


def test_high_salience_event_is_not_cooled(store: Store, paths, event_factory) -> None:
    store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))
    events = list(store.iter_events())
    _write_salience(paths, events)
    raw = json.loads(salience_file(paths).read_text(encoding="utf-8"))
    raw[COLD_ID]["score"] = 0.5  # 高于 salience_floor
    atomic_write(salience_file(paths), json.dumps(raw, ensure_ascii=False))

    assert archive_pass(paths, events, NO_STATE, None, NOW)["cooled"] == 0


def test_open_prospective_and_promoted_lesson_sources_are_never_cooled(
    store: Store, paths, event_factory
) -> None:
    open_id = store.append(event_factory(id=COLD_ID, status="open"))
    prospective_id = store.append(
        replace(event_factory(id="2026-01-05_101600", status="done", outcome="完成"), prospective=True)
    )
    lesson_id = store.append(
        event_factory(id="2026-01-05_101700", status="done", outcome="完成", lesson="端口按任务 id 错开")
    )
    state = {"runs": 1, "events": {lesson_id: {"status": "promoted", "unused_runs": 0}}}

    assert _cool(paths, store, state)["cooled"] == 0
    rows = load_archive_index(paths)
    assert open_id not in rows and prospective_id not in rows and lesson_id not in rows


def test_event_referenced_by_an_active_event_is_not_cooled(store: Store, paths, event_factory) -> None:
    """被活跃事件 parent／superseded_by 指向的事件不冷却：链目标必须可读。"""
    target = store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))
    recent = (NOW - timedelta(days=2)).strftime("%Y-%m-%d_%H%M%S")
    store.append(event_factory(id=recent, status="open", parent=target))

    assert _cool(paths, store)["cooled"] == 0


def test_cooled_events_leave_every_index(store: Store, paths, event_factory) -> None:
    cold = store.append(
        event_factory(
            id=COLD_ID,
            status="done",
            outcome="旧的结论",
            lesson="旧的教训文本",
            intent="很久以前的导出改造",
            anchors=Anchors(files=["src/export.py"]),
        )
    )
    recent = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    store.append(event_factory(id=recent, status="open", intent="最近开启的任务"))

    _cool(paths, store)
    rebuild_all(store, paths, Budget(), NOW)

    for path in (paths.project_index, paths.anchors, paths.lessons, paths.working_set):
        assert cold not in path.read_text(encoding="utf-8")
    assert "src/export.py" not in paths.anchors.read_text(encoding="utf-8")
    assert recent in paths.working_set.read_text(encoding="utf-8")  # 活跃层不受影响


def test_archive_switch_off_disables_the_whole_pass(store: Store, paths, event_factory) -> None:
    paths.config.write_text("archive: false\n", encoding="utf-8")
    store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))

    assert _cool(paths, store) == {"cooled": 0, "frozen": 0, "packs": 0}
    assert not paths.archive_index.exists()


# ==================================================================== 冻结


def test_frozen_events_are_packed_verbatim_and_removed_from_events_dir(
    store: Store, paths, event_factory
) -> None:
    event_id = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    original = paths.event_file(event_id).read_bytes()

    result = _cool(paths, store)

    assert (result["cooled"], result["frozen"], result["packs"]) == (1, 1, 1)
    assert not paths.event_file(event_id).is_file()
    pack = paths.epoch_pack(epoch_of(event_id))
    with tarfile.open(pack, "r:gz") as archive:
        assert archive.getnames() == [f"{event_id}.md"]
        assert archive.extractfile(f"{event_id}.md").read() == original  # L0 逐字节不变
    assert event_id in load_archive_index(paths)  # 归档索引留行


def test_epoch_summary_lists_members_and_falls_back_without_llm(
    store: Store, paths, event_factory
) -> None:
    event_id = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成", intent="旧的导出改造"))

    _cool(paths, store)

    text = paths.epoch_summary("2024-Q2").read_text(encoding="utf-8")
    assert "# 纪元 2024-Q2" in text
    assert "## 摘要 1" in text
    assert "旧的导出改造" in text  # client=None 时降级为 intent 拼接
    assert f"- {event_id} | 旧的导出改造" in text


def test_epoch_summary_uses_the_llm_when_available(store: Store, paths, event_factory, fake_llm) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    fake_llm.queue({"summary": "本季度的工作集中在导出模块的分页改造"})

    events = list(store.iter_events())
    _write_salience(paths, events)
    archive_pass(paths, events, NO_STATE, fake_llm, NOW)

    assert fake_llm.calls[0].system == EPOCH_SYSTEM
    assert "本季度的工作集中在导出模块的分页改造" in paths.epoch_summary("2024-Q2").read_text(encoding="utf-8")


def test_freeze_rolls_back_and_keeps_files_when_verification_fails(
    store: Store, paths, event_factory, monkeypatch
) -> None:
    event_id = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    monkeypatch.setattr(consolidate, "_verify_pack", lambda *a, **k: False)

    result = _cool(paths, store)

    assert result["frozen"] == 0 and result["packs"] == 0
    assert paths.event_file(event_id).is_file()  # 散文件原样保留
    assert not paths.all_packs()
    assert not [p for p in paths.archive_dir.iterdir() if p.name.endswith(".tmp")]


def test_event_linked_from_an_active_event_is_never_frozen(store: Store, paths, event_factory) -> None:
    target = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    # 先只冷却（此时无人引用），再造一个活跃事件指向它，随后重跑
    _cool(paths, store)
    assert not paths.event_file(target).is_file()

    store2 = Store(paths)
    thawed = store2.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    recent = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    store2.append(event_factory(id=recent, status="open", parent=thawed))

    events = list(store2.iter_events())
    _write_salience(paths, events)
    archive_pass(paths, events, NO_STATE, None, NOW)

    assert paths.event_file(thawed).is_file()  # 链目标必须可读


def test_archive_pass_is_idempotent(store: Store, paths, event_factory) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))

    first = _cool(paths, store)
    snapshot = (
        sorted(p.name for p in paths.archive_dir.iterdir()),
        paths.archive_index.read_text(encoding="utf-8"),
        sorted(p.name for p in paths.events_dir.glob("*.md")),
    )
    second = _cool(paths, store)

    assert (first["cooled"], first["frozen"]) == (2, 1)
    assert (second["cooled"], second["frozen"], second["packs"]) == (0, 0, 0)
    assert snapshot == (
        sorted(p.name for p in paths.archive_dir.iterdir()),
        paths.archive_index.read_text(encoding="utf-8"),
        sorted(p.name for p in paths.events_dir.glob("*.md")),
    )


def test_second_batch_of_the_same_quarter_goes_into_a_continuation_pack(
    store: Store, paths, event_factory
) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    _cool(paths, store)

    store.append(event_factory(id="2024-05-07_101500", status="done", outcome="完成", intent="同季度的另一件事"))
    _cool(paths, store)

    assert paths.epoch_pack("2024-Q2").is_file()
    assert paths.epoch_pack("2024-Q2", 2).is_file()
    text = paths.epoch_summary("2024-Q2").read_text(encoding="utf-8")
    assert text.count("## 摘要 ") == 2  # 摘要文件累加成员清单
    assert "同季度的另一件事" in text


def test_deep_runs_the_archive_pass_last(store: Store, paths, event_factory) -> None:
    """端到端：deep 自己完成冷却与冻结，且水位按当前 events/ 文件数推进。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    old = store.append(event_factory(id=FROZEN_ID, kind="build", status="done", outcome="完成"))
    recent = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    store.append(event_factory(id=recent, kind="build", status="done", outcome="完成"))

    deep(store, paths, Budget(), None, NOW)

    assert old in load_archive_index(paths)
    assert not paths.event_file(old).is_file()
    assert old not in paths.project_index.read_text(encoding="utf-8")
    assert paths.deep_watermark.read_text(encoding="utf-8").strip() == "1"


# ==================================================================== 检索与命令


def test_search_archive_matches_rows_and_epoch_summaries(store: Store, paths, event_factory) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成", intent="导出模块的分页改造"))
    _cool(paths, store)

    hits = search_archive("分页改造", paths)

    assert hits
    assert all(hit.line.endswith("[archived]") for hit in hits)
    assert any(hit.event_id == FROZEN_ID for hit in hits)


def test_cli_search_only_reaches_the_archive_with_all_flag(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成", intent="导出模块的分页改造"))
    _cool(paths, store)
    project = str(paths.project_dir)

    assert main(["search", "分页改造", "--project", project]) == 0
    assert "[archived]" not in capsys.readouterr().out

    assert main(["search", "分页改造", "--all", "--project", project]) == 0
    assert "[archived]" in capsys.readouterr().out


def test_cli_read_points_a_frozen_id_at_thaw(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    _cool(paths, store)

    assert main(["read", FROZEN_ID, "--project", str(paths.project_dir)]) == 1
    out = capsys.readouterr().out
    assert "已归档于 2024-Q2" in out and "thaw" in out


def test_cli_read_flags_a_link_into_the_frozen_layer(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    _cool(paths, store)
    # 冻结之后才出现的引用者：链目标已经在包里，读它的时候要给出纪元与解冻指令
    recent = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    child = store.append(event_factory(id=recent, status="done", outcome="完成", parent=FROZEN_ID))

    assert main(["read", child, "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert f"parent {FROZEN_ID} 已归档于 2024-Q2" in out


def test_cli_thaw_restores_the_event_and_restarts_its_age(store: Store, paths, event_factory, capsys) -> None:
    event_id = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    original = paths.event_file(event_id).read_bytes()
    _cool(paths, store)
    capsys.readouterr()

    assert main(["thaw", event_id, "--project", str(paths.project_dir)]) == 0

    assert paths.event_file(event_id).read_bytes() == original
    assert event_id not in load_archive_index(paths)
    assert paths.thaw_marker(event_id).is_file()
    # 年龄按解冻时间重算：紧接着再跑一次归档 pass 不会立刻把它冻回去
    assert _cool(paths, store)["cooled"] == 0
    assert paths.event_file(event_id).is_file()


def test_cli_thaw_accepts_an_epoch(store: Store, paths, event_factory, capsys) -> None:
    first = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    second = store.append(event_factory(id="2024-05-07_101500", status="done", outcome="完成"))
    _cool(paths, store)
    capsys.readouterr()

    assert main(["thaw", "2024-Q2", "--project", str(paths.project_dir)]) == 0

    assert paths.event_file(first).is_file() and paths.event_file(second).is_file()
    assert not load_archive_index(paths)


def test_cli_purge_needs_yes_and_never_touches_events_dir(store: Store, paths, event_factory, capsys) -> None:
    frozen_id = store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    cold_id = store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))
    _cool(paths, store)
    project = str(paths.project_dir)
    capsys.readouterr()

    assert main(["purge", "--before", "2025-01-01", "--project", project]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "2024-Q2" in out
    assert paths.epoch_pack("2024-Q2").is_file()  # dry-run 什么都不删

    assert main(["purge", "--before", "2025-01-01", "--yes", "--project", project]) == 0
    assert not paths.epoch_pack("2024-Q2").is_file()
    assert not paths.epoch_summary("2024-Q2").is_file()
    assert frozen_id not in load_archive_index(paths)
    assert cold_id in load_archive_index(paths)  # 未过期的纪元不受影响
    assert paths.event_file(cold_id).is_file()  # events/ 永不被 purge 触碰


def test_cli_purge_skips_quarters_that_are_not_fully_before_the_date(
    store: Store, paths, event_factory, capsys
) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))  # 2024-Q2
    _cool(paths, store)
    capsys.readouterr()

    assert main(["purge", "--before", "2024-05-20", "--project", str(paths.project_dir)]) == 0
    assert "没有已冻结的纪元" in capsys.readouterr().out
    assert paths.epoch_pack("2024-Q2").is_file()


def test_cli_stats_reports_each_layer(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(id=FROZEN_ID, status="done", outcome="完成"))
    store.append(event_factory(id=COLD_ID, status="done", outcome="完成"))
    recent = (NOW - timedelta(days=1)).strftime("%Y-%m-%d_%H%M%S")
    store.append(event_factory(id=recent, status="done", outcome="完成"))
    _cool(paths, store)
    capsys.readouterr()

    assert main(["stats", "--json", "--project", str(paths.project_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert (payload["hot_events"], payload["cold_events"], payload["frozen_events"]) == (1, 1, 1)
    assert payload["archive_packs"] == 1 and payload["archive_bytes"] > 0
    assert payload["active_ratio"] == 0.3333
