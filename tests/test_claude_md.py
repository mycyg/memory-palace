"""CLAUDE.md 晋升建议（SPEC §3.15）：promoted lesson 生成建议文件、无待采纳建议时
删除文件、已被用户采纳时移除条目并在 lessons.md 标 (adopted)、cli 的计数与提示行。
"""

from __future__ import annotations

import json
from datetime import datetime

from eventmem.cli import main as cli_main
from eventmem.consolidate import LESSON_STATE_FILE, deep
from eventmem.index import Budget, claude_md_suggestions_file
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)

LESSON_A = "并行启动多个Ray任务时端口需要按任务id错开分配，使用默认端口必然冲突"
LESSON_B = "缓存穿透时应该在应用层加空值缓存，避免每次都打到数据库"


def _promote(store: Store, paths, event_factory, lesson_text: str) -> tuple[str, str]:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    e1 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论1", lesson=lesson_text))
    e2 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论2", lesson=lesson_text))
    return e1, e2


def _bump_dirty(store: Store, event_factory) -> None:
    store.append(event_factory(status="done", outcome="无关事件", intent="推高脏量用的无关意图"))


# ==================================================================== 生成建议块


def test_promoted_lesson_generates_suggestion_block_in_expected_format(store: Store, paths, event_factory) -> None:
    e1, e2 = _promote(store, paths, event_factory, LESSON_A)

    deep(store, paths, Budget(), None, NOW)

    text = claude_md_suggestions_file(paths).read_text(encoding="utf-8")
    assert text.startswith("# CLAUDE.md 晋升建议")
    assert f"## 1. {LESSON_A}" in text
    assert f"- lesson: {LESSON_A}" in text
    assert f"- 来源: [{e1}] [{e2}]" in text
    assert f"- 可粘贴: - {LESSON_A}" in text


# ==================================================================== 无待采纳建议时文件被删除


def test_suggestions_file_is_deleted_once_the_lesson_is_retired(store: Store, paths, event_factory) -> None:
    e1, e2 = _promote(store, paths, event_factory, LESSON_A)
    deep(store, paths, Budget(), None, NOW)
    assert claude_md_suggestions_file(paths).exists()

    state_path = paths.log_dir / LESSON_STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["events"][e1]["status"] = "retired"
    state["events"][e2]["status"] = "retired"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    _bump_dirty(store, event_factory)
    deep(store, paths, Budget(), None, NOW)

    assert not claude_md_suggestions_file(paths).exists()


# ==================================================================== 已被用户采纳


def test_claude_md_containing_the_suggestion_marks_source_events_adopted(
    store: Store, paths, event_factory
) -> None:
    e1, e2 = _promote(store, paths, event_factory, LESSON_A)
    deep(store, paths, Budget(), None, NOW)
    assert claude_md_suggestions_file(paths).exists()

    (paths.project_dir / "CLAUDE.md").write_text(f"# 项目说明\n\n- {LESSON_A}\n", encoding="utf-8")
    _bump_dirty(store, event_factory)
    deep(store, paths, Budget(), None, NOW)

    assert not claude_md_suggestions_file(paths).exists()  # 唯一一条建议已被采纳，文件被清理
    lessons_text = paths.lessons.read_text(encoding="utf-8")
    assert f"- [{e1}] (adopted)" in lessons_text
    assert f"- [{e2}] (adopted)" in lessons_text


# ==================================================================== cli：status 与 stats


def test_cli_status_shows_pending_suggestion_count_and_hides_when_none(
    store: Store, paths, event_factory, capsys
) -> None:
    _promote(store, paths, event_factory, LESSON_A)
    deep(store, paths, Budget(), None, NOW)
    capsys.readouterr()

    assert cli_main(["status", "--project", str(paths.project_dir)]) == 0
    assert "未读 CLAUDE.md 建议: 1 条" in capsys.readouterr().out


def test_cli_status_omits_suggestion_line_when_there_are_none(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(status="done", outcome="没有教训的普通事件"))
    capsys.readouterr()

    assert cli_main(["status", "--project", str(paths.project_dir)]) == 0
    assert "CLAUDE.md 建议" not in capsys.readouterr().out


def test_cli_stats_json_reports_two_pending_suggestions_as_two_heading_blocks(
    store: Store, paths, event_factory, capsys
) -> None:
    _promote(store, paths, event_factory, LESSON_A)
    e3 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论3", lesson=LESSON_B))
    e4 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论4", lesson=LESSON_B))
    deep(store, paths, Budget(), None, NOW)
    capsys.readouterr()

    assert cli_main(["stats", "--json", "--project", str(paths.project_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["claude_md_suggestions_pending"] == 2
    text = claude_md_suggestions_file(paths).read_text(encoding="utf-8")
    assert text.count("## ") == 2
    assert e3 and e4  # 消除未使用变量告警


def test_cli_stats_text_mode_shows_the_suggestion_hint_line(store: Store, paths, event_factory, capsys) -> None:
    _promote(store, paths, event_factory, LESSON_A)
    deep(store, paths, Budget(), None, NOW)
    capsys.readouterr()

    assert cli_main(["stats", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "提示: 有 1 条待处理的 CLAUDE.md 晋升建议" in out
