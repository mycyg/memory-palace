"""index.py：L1 索引的重建、原子替换、文件格式与预算纪律。"""

from __future__ import annotations

from datetime import datetime

from eventmem.index import (
    Budget,
    INTENT_COLUMN_CHARS,
    iter_anchor_keys,
    load_anchor_map,
    load_lesson_states,
    rebuild_all,
)
from eventmem.schema import Anchors
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)

# ---------------------------------------------------------------- 幂等 ＋ 原子替换


def test_rebuild_all_is_byte_for_byte_idempotent(store: Store, paths, event_factory) -> None:
    store.append(event_factory(kind="fix", status="open", intent="修复端口冲突"))
    store.append(
        event_factory(
            kind="explore",
            status="abandoned",
            intent="尝试向量检索",
            outcome="放弃：复杂度过高",
            lesson="小规模数据不需要向量库",
        )
    )
    budget = Budget()

    rebuild_all(store, paths, budget, NOW)
    snapshot_1 = {
        f: (paths.index_dir / f).read_bytes()
        for f in ("project.md", "anchors.json", "lessons.md", "working-set.md")
    }
    rebuild_all(store, paths, budget, NOW)
    snapshot_2 = {
        f: (paths.index_dir / f).read_bytes()
        for f in ("project.md", "anchors.json", "lessons.md", "working-set.md")
    }

    assert snapshot_1 == snapshot_2


def test_rebuild_all_leaves_no_tmp_files_behind(store: Store, paths, event_factory) -> None:
    store.append(event_factory(intent="任意事件"))
    rebuild_all(store, paths, Budget(), NOW)

    leftovers = [p.name for p in paths.index_dir.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".")]
    assert leftovers == []


def test_rebuild_all_replaces_atomically_when_files_preexist(store: Store, paths, event_factory) -> None:
    """写入前索引文件已存在时，重建后旧内容被完全替换而不是追加。"""
    paths.project_index.write_text("陈旧的残留内容\n", encoding="utf-8")
    store.append(event_factory(intent="新事件"))
    rebuild_all(store, paths, Budget(), NOW)

    content = paths.project_index.read_text(encoding="utf-8")
    assert "陈旧的残留内容" not in content
    assert "新事件" in content


# ---------------------------------------------------------------- project.md 格式


def test_project_index_header_and_row_format(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(kind="fix", status="done", intent="修复端口冲突"))
    rebuild_all(store, paths, Budget(), NOW)

    lines = paths.project_index.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "| id | kind | status | intent |"
    assert lines[1] == "|---|---|---|---|"
    assert lines[2] == f"| {e} | fix | done | 修复端口冲突 |"


def test_project_index_truncates_long_intent_to_80_chars(store: Store, paths, event_factory) -> None:
    long_intent = "A" * 100
    store.append(event_factory(intent=long_intent))
    rebuild_all(store, paths, Budget(), NOW)

    data_line = paths.project_index.read_text(encoding="utf-8").splitlines()[2]
    intent_cell = data_line.strip("|").split("|")[3].strip()

    assert len(intent_cell) == INTENT_COLUMN_CHARS
    assert intent_cell.endswith("…")
    assert intent_cell[:-1] == "A" * (INTENT_COLUMN_CHARS - 1)


def test_project_index_flattens_multiline_intent_to_single_line(store: Store, paths, event_factory) -> None:
    store.append(event_factory(intent="第一行\n第二行\t带制表符"))
    rebuild_all(store, paths, Budget(), NOW)

    data_line = paths.project_index.read_text(encoding="utf-8").splitlines()[2]
    assert "\n" not in data_line
    assert "第一行 第二行 带制表符" in data_line


# ---------------------------------------------------------------- anchors.json 倒排


def test_anchor_map_covers_file_error_intent_key_types(store: Store, paths, event_factory) -> None:
    e = store.append(
        event_factory(
            kind="fix",
            status="open",
            intent="修复端口冲突",
            anchors=Anchors(files=["train/launcher.py"], error_sigs=["ValueError: port busy"]),
        )
    )
    rebuild_all(store, paths, Budget(), NOW)
    anchor_map = load_anchor_map(paths)

    assert anchor_map["file:train/launcher.py"] == [e]
    assert anchor_map["error:ValueError: port busy"] == [e]
    for token in ("修复", "端口", "口冲", "冲突"):
        assert anchor_map[f"intent:{token}"] == [e]

    prefixes = {key.split(":", 1)[0] for key in anchor_map}
    assert prefixes == {"file", "error", "intent"}


def test_anchor_map_matches_iter_anchor_keys_helper(store: Store, paths, event_factory) -> None:
    """anchors.json 的 key 集合应与 iter_anchor_keys（供增量更新复用的同一套逻辑）一致。"""
    e_obj = event_factory(
        intent="重构缓存层",
        anchors=Anchors(files=["src/cache.py"], error_sigs=["KeyError: missing"]),
    )
    store.append(e_obj)
    rebuild_all(store, paths, Budget(), NOW)
    anchor_map = load_anchor_map(paths)

    expected_keys = set(iter_anchor_keys(e_obj, paths))
    for key in expected_keys:
        assert e_obj.id in anchor_map[key]


def test_anchor_map_aggregates_multiple_events_under_shared_key(store: Store, paths, event_factory) -> None:
    e1 = store.append(event_factory(anchors=Anchors(files=["shared.py"])))
    e2 = store.append(event_factory(anchors=Anchors(files=["shared.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    anchor_map = load_anchor_map(paths)
    assert anchor_map["file:shared.py"] == sorted([e1, e2])


def test_load_anchor_map_returns_empty_dict_when_file_missing(paths) -> None:
    assert load_anchor_map(paths) == {}


# ---------------------------------------------------------------- working-set 预算与填充优先级


def test_working_set_prioritizes_open_events_over_lessons_and_outcomes(store: Store, paths, event_factory) -> None:
    """极小预算下：只有 open 事件（且只有装得下的那部分）能进工作集，
    lesson 与 outcome 完全挤不进去——即便其中一条 lesson 已经晋升。"""
    for i in range(3):
        store.append(event_factory(kind="build", status="open", intent=f"开放事件{i}"))
    lesson_event = store.append(
        event_factory(kind="fix", status="abandoned", outcome="结论", lesson="L" * 5000)
    )
    outcome_event = store.append(event_factory(kind="build", status="done", outcome="O" * 5000))

    # 先跑一遍让 lessons.md 生成，再手工把该 lesson 标成 promoted
    rebuild_all(store, paths, Budget(), NOW)
    content = paths.lessons.read_text(encoding="utf-8").replace("(candidate)", "(promoted)")
    paths.lessons.write_text(content, encoding="utf-8")
    assert load_lesson_states(paths)[lesson_event] == "promoted"

    tiny_budget = Budget(working_set_tokens=50)
    rebuild_all(store, paths, tiny_budget, NOW)
    ws = paths.working_set.read_text(encoding="utf-8")

    assert "开放事件2" in ws  # 最新的 open 事件优先保留
    assert lesson_event not in ws  # promoted lesson 也挤不进极小预算
    assert outcome_event not in ws
    # Recent outcomes / Lessons 两节标题仍渲染，但节内没有任何一行内容
    assert ws.split("## Recent outcomes")[1].split("## Lessons")[0].strip() == ""
    assert ws.rsplit("## Lessons (promoted)", 1)[1].strip() == ""


def test_working_set_fills_promoted_lessons_before_recent_outcomes(store: Store, paths, event_factory) -> None:
    """中等预算：三条 open 事件 ＋ 一条已晋升的短 lesson 都能进去，
    但一条超长 outcome 装不下——验证 lesson 的填充优先级高于 outcome。"""
    for i in range(3):
        store.append(event_factory(kind="build", status="open", intent=f"开放事件{i}"))
    lesson_event = store.append(
        event_factory(
            kind="fix",
            status="abandoned",
            outcome="结论",
            lesson="一个中等长度但明确可复用的教训文本用来测试优先级",
        )
    )
    outcome_event = store.append(event_factory(kind="build", status="done", outcome="O" * 5000))

    rebuild_all(store, paths, Budget(), NOW)
    content = paths.lessons.read_text(encoding="utf-8").replace("(candidate)", "(promoted)")
    paths.lessons.write_text(content, encoding="utf-8")

    medium_budget = Budget(working_set_tokens=120)
    rebuild_all(store, paths, medium_budget, NOW)
    ws = paths.working_set.read_text(encoding="utf-8")

    assert "开放事件0" in ws and "开放事件1" in ws and "开放事件2" in ws
    assert lesson_event in ws
    assert "一个中等长度但明确可复用的教训文本用来测试优先级" in ws
    assert outcome_event not in ws


def test_working_set_header_reports_generation_timestamp(store: Store, paths, event_factory) -> None:
    store.append(event_factory())
    rebuild_all(store, paths, Budget(), NOW)
    first_line = paths.working_set.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == f"# Memory working set (generated {NOW.isoformat(timespec='seconds')})"


# ---------------------------------------------------------------- lessons.md 状态保留


def test_lessons_md_preserves_promoted_state_across_rebuild(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(status="abandoned", outcome="结论", lesson="可复用的教训"))
    rebuild_all(store, paths, Budget(), NOW)
    assert "(candidate)" in paths.lessons.read_text(encoding="utf-8")

    content = paths.lessons.read_text(encoding="utf-8").replace("(candidate)", "(promoted)")
    paths.lessons.write_text(content, encoding="utf-8")
    assert load_lesson_states(paths) == {e: "promoted"}

    # 再新增一个不相关事件后重建：旧事件的 promoted 状态应原样保留
    store.append(event_factory(intent="另一件不相关的事"))
    rebuild_all(store, paths, Budget(), NOW)

    lessons_text = paths.lessons.read_text(encoding="utf-8")
    assert f"- [{e}] (promoted) 可复用的教训" in lessons_text


def test_lessons_md_defaults_new_lessons_to_candidate(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome="完成", lesson="全新的教训"))
    rebuild_all(store, paths, Budget(), NOW)
    assert f"- [{e}] (candidate) 全新的教训" in paths.lessons.read_text(encoding="utf-8")


def test_lessons_md_omits_events_without_lesson(store: Store, paths, event_factory) -> None:
    store.append(event_factory(status="done", outcome="完成", lesson=None))
    rebuild_all(store, paths, Budget(), NOW)
    body = paths.lessons.read_text(encoding="utf-8")
    assert body.strip() == "# Lessons"
