"""跨项目的用户级 lesson 晋升（SPEC §3.18）：可移植性判定、跨项目近似晋升、
global-lessons.md 只含 promoted、目录不存在时静默跳过、working-set 的独立预算。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from eventmem.consolidate import PORTABILITY_SYSTEM, deep
from eventmem.index import Budget, GLOBAL_LESSONS_FILE, GLOBAL_STATE_FILE, load_global_lessons, rebuild_all
from eventmem.paths import MemoryPaths
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)

LESSON_TEXT = "并行启动多个Ray任务时端口需要按任务id错开分配，使用默认端口必然冲突"


def _promote_local_lesson(store: Store, paths, event_factory) -> tuple[str, str]:
    """造两个带相同 lesson 文本的 abandoned/fix 事件，让它们在本项目内先晋升为 promoted。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    e1 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论1", lesson=LESSON_TEXT))
    e2 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论2", lesson=LESSON_TEXT))
    return e1, e2


# ==================================================================== 目录不存在：静默跳过且不创建


def test_promotion_pass_is_skipped_silently_when_global_dir_does_not_exist(
    store: Store, paths, event_factory, fake_llm, global_lesson_dir: Path
) -> None:
    assert not global_lesson_dir.exists()
    _promote_local_lesson(store, paths, event_factory)

    deep(store, paths, Budget(), fake_llm, NOW)  # 队列为空：真调用 LLM 就会 AssertionError

    assert not global_lesson_dir.exists()  # eventmem 本身永不创建这个目录


# ==================================================================== 可移植性判定


def test_portability_check_true_registers_project_as_a_global_candidate(
    store: Store, paths, event_factory, fake_llm, global_lesson_dir: Path
) -> None:
    global_lesson_dir.mkdir(parents=True)
    e1, e2 = _promote_local_lesson(store, paths, event_factory)
    fake_llm.queue({e1: True, e2: True})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[-1].system == PORTABILITY_SYSTEM
    state = json.loads((global_lesson_dir / GLOBAL_STATE_FILE).read_text(encoding="utf-8"))
    assert len(state["candidates"]) == 1
    candidate = state["candidates"][0]
    assert candidate["promoted"] is False  # 只出现在 1 个项目里，还不够跨项目晋升门槛
    # 两个来源事件属于同一个项目，去重后只登记一次
    assert len(candidate["projects"]) == 1
    assert LESSON_TEXT[:20] in candidate["text"]


def test_portability_check_false_does_not_create_a_candidate(
    store: Store, paths, event_factory, fake_llm, global_lesson_dir: Path
) -> None:
    global_lesson_dir.mkdir(parents=True)
    e1, e2 = _promote_local_lesson(store, paths, event_factory)
    fake_llm.queue({e1: False, e2: False})

    deep(store, paths, Budget(), fake_llm, NOW)

    # 判定结果全部为 False：没有任何变化发生，两个 global 文件都不应被创建出来
    assert not (global_lesson_dir / GLOBAL_STATE_FILE).exists()
    assert not (global_lesson_dir / GLOBAL_LESSONS_FILE).exists()
    assert load_global_lessons() == []


# ==================================================================== 跨项目 ≥2 近似晋升


def test_lesson_seen_in_two_different_projects_gets_promoted_to_global(
    tmp_path: Path, event_factory, global_lesson_dir: Path, fake_llm
) -> None:
    """一个 fake_llm 实例按调用顺序依次服务两个不同项目的 deep() 调用即可——
    它只是一个响应队列，与具体项目路径无关。"""
    global_lesson_dir.mkdir(parents=True)

    paths1 = MemoryPaths.for_project(tmp_path / "project1")
    paths1.ensure()
    store1 = Store(paths1)
    e1, e2 = _promote_local_lesson(store1, paths1, event_factory)
    fake_llm.queue({e1: True, e2: True})
    deep(store1, paths1, Budget(), fake_llm, NOW)

    state_after_project1 = json.loads((global_lesson_dir / GLOBAL_STATE_FILE).read_text(encoding="utf-8"))
    assert state_after_project1["candidates"][0]["promoted"] is False

    paths2 = MemoryPaths.for_project(tmp_path / "project2")
    paths2.ensure()
    store2 = Store(paths2)
    e3, e4 = _promote_local_lesson(store2, paths2, event_factory)
    fake_llm.queue({e3: True, e4: True})
    deep(store2, paths2, Budget(), fake_llm, NOW)

    state_after_project2 = json.loads((global_lesson_dir / GLOBAL_STATE_FILE).read_text(encoding="utf-8"))
    assert len(state_after_project2["candidates"]) == 1
    candidate = state_after_project2["candidates"][0]
    assert candidate["promoted"] is True
    assert len(set(candidate["projects"])) == 2

    lessons_md = (global_lesson_dir / GLOBAL_LESSONS_FILE).read_text(encoding="utf-8")
    assert "(promoted)" in lessons_md
    assert LESSON_TEXT[:20] in lessons_md


# ==================================================================== global-lessons.md 只含 promoted


def test_load_global_lessons_filters_out_non_promoted_entries(global_lesson_dir: Path) -> None:
    global_lesson_dir.mkdir(parents=True)
    (global_lesson_dir / GLOBAL_LESSONS_FILE).write_text(
        "# Global lessons (user level)\n\n"
        "- (candidate) 这条不该被注入\n"
        "- (promoted) 这条应该被注入\n"
        "- (retired) 这条已退休不该被注入\n",
        encoding="utf-8",
    )

    texts = load_global_lessons()

    assert texts == ["这条应该被注入"]


def test_load_global_lessons_returns_empty_list_when_directory_missing(global_lesson_dir: Path) -> None:
    assert not global_lesson_dir.exists()
    assert load_global_lessons() == []


# ==================================================================== config.yml: global_lessons: false


def test_config_global_lessons_false_disables_the_whole_promotion_pass(
    store: Store, paths, event_factory, fake_llm, global_lesson_dir: Path
) -> None:
    global_lesson_dir.mkdir(parents=True)
    paths.config.write_text("deep_threshold: 1\nglobal_lessons: false\n", encoding="utf-8")
    e1 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论1", lesson=LESSON_TEXT))
    e2 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论2", lesson=LESSON_TEXT))

    deep(store, paths, Budget(), fake_llm, NOW)  # 队列为空：真调用 LLM 就会 AssertionError

    assert not (global_lesson_dir / GLOBAL_STATE_FILE).exists()
    assert e1 and e2  # 消除未使用变量告警


# ==================================================================== working-set：Lessons (global) 独立预算


def test_working_set_renders_global_lessons_independent_of_regular_budget(
    store: Store, paths, global_lesson_dir: Path
) -> None:
    global_lesson_dir.mkdir(parents=True)
    (global_lesson_dir / GLOBAL_LESSONS_FILE).write_text(
        "# Global lessons (user level)\n\n"
        "- (promoted) 第一条全局教训\n"
        "- (promoted) 第二条全局教训\n"
        "- (promoted) 第三条全局教训\n"
        "- (promoted) " + "X" * 700 + "\n",  # 单条即超过 200 token 独立预算
        encoding="utf-8",
    )
    tiny_budget = Budget(working_set_tokens=10)  # 常规工作集预算极小，几乎装不下任何常规内容

    rebuild_all(store, paths, tiny_budget, NOW)

    ws = paths.working_set.read_text(encoding="utf-8")
    assert "## Lessons (global)" in ws
    assert "第一条全局教训" in ws
    assert "第二条全局教训" in ws
    assert "第三条全局教训" in ws
    assert "X" * 700 not in ws  # 超过全局区自己的 200 token 上限，被截断


def test_working_set_omits_global_lessons_heading_when_none_available(store: Store, paths) -> None:
    rebuild_all(store, paths, Budget(), NOW)
    assert "## Lessons (global)" not in paths.working_set.read_text(encoding="utf-8")
