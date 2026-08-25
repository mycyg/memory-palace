"""cli.py 的观测类命令（SPEC §3.13）：`eventmem stats` 的各字段与优雅降级、
`eventmem log` 的 --tree/--since/--kind 与不可解析 id 的 fail-closed。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from eventmem.cli import main as cli_main
from eventmem.schema import Anchors, make_event, new_id, to_markdown
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)


def _write_jsonl(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


# ==================================================================== stats：完整数据下的各字段


def test_stats_json_reports_every_field_with_full_data(store: Store, paths, event_factory, capsys) -> None:
    e1 = store.append(event_factory(kind="fix", status="done", outcome="修复1", anchors=Anchors(error_sigs=["SIG_X"])))
    e2 = store.append(event_factory(kind="fix", status="done", outcome="修复2", anchors=Anchors(error_sigs=["SIG_X"])))
    store.append(event_factory(kind="fix", status="done", outcome="修复3", anchors=Anchors(error_sigs=["SIG_Y"])))
    store.append(event_factory(kind="build", status="done", outcome="构建", anchors=Anchors(error_sigs=["SIG_X"])))
    store.append(event_factory(kind="decision", status="open"))

    _write_jsonl(paths.log_dir / "surfaced-sessA.jsonl", [{"ts": "x", "event_id": e1, "cue": "a", "cue_kind": "file", "chars": 1}])
    _write_jsonl(paths.log_dir / "surfaced-sessB.jsonl.done", [{"ts": "x", "event_id": e2, "cue": "b", "cue_kind": "file", "chars": 1}])
    _write_jsonl(paths.log_dir / "injected-sessA.jsonl", [{"ts": "x", "source": "working-set", "chars": 100}])
    _write_jsonl(paths.log_dir / "injected-sessB.jsonl", [{"ts": "x", "source": "working-set", "chars": 50}])
    (paths.index_dir).mkdir(parents=True, exist_ok=True)
    (paths.index_dir / "salience.json").write_text(
        json.dumps(
            {
                e1: {"score": 0.5, "prior": "medium", "evidence": {"refs": 0, "hits": 3, "ignored": 1, "superseded_trigger": False}, "updated": "x"},
                e2: {"score": 0.5, "prior": "medium", "evidence": {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False}, "updated": "x"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(paths.log_dir / "prefetch-outcome.jsonl", [{"session": "sessA", "predicted": 5, "hit": 2}, {"session": "sessB", "predicted": 3, "hit": 1}])
    (paths.index_dir / "claude-md-suggestions.md").write_text(
        "# CLAUDE.md 晋升建议\n\n## 1. 第一条\n\n- lesson: x\n\n## 2. 第二条\n\n- lesson: y\n", encoding="utf-8"
    )
    capsys.readouterr()

    assert cli_main(["stats", "--json", "--project", str(paths.project_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["events_total"] == 5
    assert payload["by_kind"] == {"build": 1, "decision": 1, "fix": 3}
    assert payload["by_status"] == {"done": 4, "open": 1}
    assert payload["surfaced_total"] == 2  # .jsonl 与 .jsonl.done 都被计入
    assert payload["adoption_hits"] == 3
    assert payload["adoption_ignored"] == 1
    assert payload["adoption_rate"] == 0.75
    assert payload["injected_chars_total"] == 150
    assert payload["repeated_pitfalls"] == 1  # 只有 SIG_X 关联了 ≥2 个 fix 事件
    assert payload["prefetch_predicted"] == 8
    assert payload["prefetch_hit"] == 3
    assert payload["prefetch_hit_rate"] == 0.375
    assert payload["claude_md_suggestions_pending"] == 2
    assert payload["hot_events"] == 5
    assert payload["cold_events"] == 0
    assert payload["frozen_events"] == 0
    assert payload["events_all_layers"] == 5
    assert payload["archive_packs"] == 0
    assert payload["archive_bytes"] == 0
    assert payload["active_ratio"] == 1.0


def test_stats_text_mode_shows_the_same_rates(store: Store, paths, event_factory, capsys) -> None:
    e1 = store.append(event_factory(status="done", outcome="结论", anchors=Anchors(files=["a.py"])))
    (paths.index_dir).mkdir(parents=True, exist_ok=True)
    (paths.index_dir / "salience.json").write_text(
        json.dumps({e1: {"score": 0.5, "prior": "medium", "evidence": {"refs": 0, "hits": 1, "ignored": 1, "superseded_trigger": False}, "updated": "x"}}),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert cli_main(["stats", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "事件总数: 1" in out
    assert "采纳率: 50.0% (1/2)" in out


# ==================================================================== stats：缺文件全 n/a 不炸


def test_stats_degrades_to_na_when_derived_files_are_all_missing(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(status="done", outcome="唯一的事件"))
    capsys.readouterr()

    assert cli_main(["stats", "--json", "--project", str(paths.project_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["adoption_hits"] is None
    assert payload["adoption_ignored"] is None
    assert payload["adoption_rate"] is None
    assert payload["prefetch_predicted"] is None
    assert payload["prefetch_hit"] is None
    assert payload["prefetch_hit_rate"] is None
    assert payload["surfaced_total"] == 0
    assert payload["injected_chars_total"] == 0
    assert payload["claude_md_suggestions_pending"] == 0


def test_stats_text_mode_shows_na_for_missing_rates(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(status="open"))
    capsys.readouterr()

    assert cli_main(["stats", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "采纳率: n/a" in out
    assert "预取命中率: n/a" in out


def test_stats_does_not_crash_on_a_completely_fresh_project(paths, capsys) -> None:
    """连事件目录都还没有任何文件的全新项目：stats 也不应该报错。"""
    assert cli_main(["stats", "--json", "--project", str(paths.project_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events_total"] == 0
    assert payload["active_ratio"] is None  # 0/0 时占比也降级为 n/a（json 里是 null）


# ==================================================================== log：基本输出与过滤


def test_log_prints_flat_list_without_indentation(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(kind="fix", status="done", outcome="x", intent="第一件事"))
    store.append(event_factory(kind="build", status="open", intent="第二件事"))
    capsys.readouterr()

    assert cli_main(["log", "--project", str(paths.project_dir)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    assert len(lines) == 2
    assert not lines[0].startswith(" ")
    assert "第一件事" in lines[0] and "fix done" in lines[0]
    assert "第二件事" in lines[1] and "build open" in lines[1]


def test_log_tree_indents_by_parent_depth(store: Store, paths, event_factory, capsys) -> None:
    grandparent = store.append(event_factory(status="done", outcome="x", intent="祖先任务"))
    parent = store.append(event_factory(status="done", outcome="x", intent="父任务", parent=grandparent))
    store.append(event_factory(status="open", intent="子任务", parent=parent))
    capsys.readouterr()

    assert cli_main(["log", "--tree", "--project", str(paths.project_dir)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    assert "祖先任务" in lines[0] and not lines[0].startswith(" ")  # 深度 0：无缩进
    assert "父任务" in lines[1] and lines[1].startswith("  ") and not lines[1].startswith("    ")  # 深度 1
    assert "子任务" in lines[2] and lines[2].startswith("    ")  # 深度 2：两级缩进


def test_log_tree_handles_missing_parent_without_crashing(store: Store, paths, event_factory, capsys) -> None:
    orphan = store.append(event_factory(status="open", intent="孤儿事件", parent="2000-01-01_000000"))
    capsys.readouterr()

    assert cli_main(["log", "--tree", "--project", str(paths.project_dir)]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines[0].startswith(orphan)  # 找不到的 parent 不阻断输出，深度按 0 处理，无缩进


def test_log_kind_filters_to_matching_events_only(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(kind="fix", status="done", outcome="x", intent="修复类事件"))
    store.append(event_factory(kind="build", status="open", intent="构建类事件"))
    capsys.readouterr()

    assert cli_main(["log", "--kind", "fix", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "修复类事件" in out
    assert "构建类事件" not in out


def test_log_prints_placeholder_when_kind_filter_matches_nothing(store: Store, paths, event_factory, capsys) -> None:
    store.append(event_factory(kind="build", status="open"))
    capsys.readouterr()

    assert cli_main(["log", "--kind", "nonexistent_kind", "--project", str(paths.project_dir)]) == 0
    assert capsys.readouterr().out.strip() == "(无匹配事件)"


def test_log_since_filters_by_id_timestamp(store: Store, paths, event_factory, capsys) -> None:
    recent_id = new_id(datetime.now() - timedelta(days=1), set())
    old_id = new_id(datetime.now() - timedelta(days=100), set())
    store.append(event_factory(id=recent_id, status="done", outcome="x", intent="最近的事件"))
    store.append(event_factory(id=old_id, status="done", outcome="x", intent="很久以前的事件"))
    capsys.readouterr()

    assert cli_main(["log", "--since", "7", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "最近的事件" in out
    assert "很久以前的事件" not in out


# ==================================================================== log：不可解析 id 的 fail-closed


def test_log_since_excludes_events_with_unparseable_id(store: Store, paths, event_factory, capsys) -> None:
    """id 无法解析时间戳时保守排除（宁漏勿胀），而不是当作「一定在范围内」放行。"""
    recent_id = new_id(datetime.now() - timedelta(days=1), set())
    store.append(event_factory(id=recent_id, status="done", outcome="x", intent="正常可解析的事件"))
    weird = make_event("weird-id-123", "fix", "open", "不可解析id的事件")
    paths.event_file("weird-id-123").write_text(to_markdown(weird), encoding="utf-8")
    capsys.readouterr()

    assert cli_main(["log", "--since", "7", "--project", str(paths.project_dir)]) == 0
    out = capsys.readouterr().out
    assert "正常可解析的事件" in out
    assert "不可解析id的事件" not in out


def test_log_without_since_still_shows_events_with_unparseable_id(store: Store, paths, event_factory, capsys) -> None:
    """fail-closed 只发生在 --since 过滤路径上；不加 --since 时不可解析 id 的事件照常显示。"""
    weird = make_event("weird-id-123", "fix", "open", "不可解析id的事件")
    paths.event_file("weird-id-123").write_text(to_markdown(weird), encoding="utf-8")
    capsys.readouterr()

    assert cli_main(["log", "--project", str(paths.project_dir)]) == 0
    assert "不可解析id的事件" in capsys.readouterr().out
