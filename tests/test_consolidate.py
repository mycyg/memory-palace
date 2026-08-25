"""consolidate.py：轻整理（light）与深整理（deep）。

安全纪律贯穿全文件的隐含前提：两级整理只写 L1 派生文件、lesson 字段、规则闭合
与 stale 标注，不改 intent/body（已闭合但缺 outcome 的补全通道除外）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from eventmem.consolidate import (
    DEFAULT_DEEP_THRESHOLD,
    LESSON_SYSTEM,
    OUTCOME_SYSTEM,
    deep,
    dirty_count,
    light,
)
from eventmem.extract import save_todo_state
from eventmem.index import Budget
from eventmem.schema import new_id
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)

# ==================================================================== light


def test_light_closes_event_when_todo_state_marks_completed(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(status="open", intent="构建导出功能"))
    save_todo_state(
        paths,
        {"构建导出功能": {"status": "completed", "event_id": e, "text": "构建导出功能", "session": "s1", "line": 9}},
    )

    light(store, paths, Budget(), None, NOW)

    closed = store.read(e)
    assert closed.status == "done"
    assert closed.outcome  # client=None 时降级复制 intent，但必须非空


def test_light_ignores_todo_state_entries_whose_event_is_not_open(store: Store, paths, event_factory) -> None:
    """todo-state 指向的事件如果已经不是 open（比如已被别的路径关闭），规则闭合不应报错或覆盖。"""
    e = store.append(event_factory(status="done", outcome="早已完成"))
    save_todo_state(paths, {"key": {"status": "completed", "event_id": e}})

    light(store, paths, Budget(), None, NOW)  # 不应抛异常

    result = store.read(e)
    assert result.status == "done"
    assert result.outcome == "早已完成"


def test_light_fills_missing_outcome_via_llm_without_changing_intent_or_body(
    store: Store, paths, event_factory, fake_llm
) -> None:
    e = store.append(
        event_factory(status="abandoned", outcome=None, intent="修复缓存穿透", body="原始正文不应被修改")
    )
    # 第二个响应给 v0.2 的显著性先验补评（outcome 补写之后跑），避免它取到空队列
    fake_llm.queue({e: "缓存穿透问题已放弃处理"}, {e: {"prior": "medium", "reason": "缓存穿透的成因可复用"}})

    original = store.read(e)
    set_outcome_spy = MagicMock(wraps=store.set_outcome)
    store.set_outcome = set_outcome_spy  # type: ignore[method-assign]

    light(store, paths, Budget(), fake_llm, NOW)

    result = store.read(e)
    assert result.outcome == "缓存穿透问题已放弃处理"
    assert result.intent == original.intent
    assert result.body == original.body
    assert result.status == "abandoned"  # 补写 outcome 不应改动 status
    set_outcome_spy.assert_called_once_with(e, "缓存穿透问题已放弃处理")


def test_light_falls_back_to_intent_copy_when_client_is_none(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome=None, intent="修复X问题"))
    light(store, paths, Budget(), None, NOW)
    assert store.read(e).outcome == "修复X问题"


def test_light_does_not_touch_events_that_already_have_outcome(store: Store, paths, event_factory, fake_llm) -> None:
    """v0.2 起 light 还会给已闭合事件补显著性先验，因此不能再断言「一次 LLM 都不调」；
    改为断言 outcome 补写这一步没被触发（队列里也只放先验补评的响应）。"""
    e = store.append(event_factory(status="done", outcome="已有的结论"))
    fake_llm.queue({e: {"prior": "low", "reason": "常规构建，结果即代码本身"}})

    light(store, paths, Budget(), fake_llm, NOW)

    assert store.read(e).outcome == "已有的结论"
    assert all(call.system != OUTCOME_SYSTEM for call in fake_llm.calls)


def test_light_rebuilds_working_set(store: Store, paths, event_factory) -> None:
    store.append(event_factory(status="open", intent="进行中的任务"))
    assert not paths.working_set.exists()

    light(store, paths, Budget(), None, NOW)

    assert paths.working_set.exists()
    content = paths.working_set.read_text(encoding="utf-8")
    assert f"generated {NOW.isoformat(timespec='seconds')}" in content
    assert "进行中的任务" in content


# ==================================================================== deep：脏量阈值


def test_dirty_count_equals_total_events_without_prior_watermark(store: Store, paths, event_factory) -> None:
    for _ in range(3):
        store.append(event_factory())
    assert dirty_count(paths) == 3


def test_dirty_count_subtracts_prior_deep_watermark(store: Store, paths, event_factory) -> None:
    for _ in range(5):
        store.append(event_factory())
    paths.deep_watermark.write_text("3", encoding="utf-8")
    assert dirty_count(paths) == 2


def test_deep_skips_llm_work_when_dirty_below_threshold(store: Store, paths, event_factory, fake_llm) -> None:
    for _ in range(3):
        store.append(event_factory(status="abandoned", outcome="放弃", lesson=None))
    assert dirty_count(paths) < DEFAULT_DEEP_THRESHOLD

    deep(store, paths, Budget(), fake_llm, NOW)  # 队列为空：真被调用就会 AssertionError

    assert fake_llm.call_count == 0
    assert not paths.deep_watermark.exists()


# ==================================================================== deep：蒸馏候选筛选


def _use_low_deep_threshold(memory_paths) -> None:
    """写入 deep_threshold=1，让脏量判定几乎总是达标，专注测试后续步骤。"""
    memory_paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")


def test_deep_lesson_candidates_are_abandoned_or_fix_without_existing_lesson(
    store: Store, paths, event_factory, fake_llm
) -> None:
    _use_low_deep_threshold(paths)

    abandoned_no_lesson = store.append(
        event_factory(kind="build", status="abandoned", outcome="放弃", lesson=None, intent="放弃的构建-无教训")
    )
    fix_no_lesson = store.append(
        event_factory(kind="fix", status="done", outcome="已修复", lesson=None, intent="修复的bug-无教训")
    )
    open_build_no_lesson = store.append(
        event_factory(kind="build", status="open", lesson=None, intent="进行中的构建")
    )
    abandoned_has_lesson = store.append(
        event_factory(
            kind="fix", status="abandoned", outcome="放弃", lesson="已经存在的教训文本", intent="已有教训的放弃事件"
        )
    )
    # 第二个响应给 v0.2 的模型级预取（本例有一个 open 事件，所以它会跑）
    fake_llm.queue({abandoned_no_lesson: None, fix_no_lesson: None}, {"predictions": []})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[0].system == LESSON_SYSTEM  # 蒸馏是 deep 的第一次 LLM 调用
    payload = fake_llm.calls[0].user
    assert abandoned_no_lesson in payload
    assert fix_no_lesson in payload
    assert open_build_no_lesson not in payload  # 非 abandoned 且非 fix
    assert abandoned_has_lesson not in payload  # 已有 lesson，不需要再蒸馏


def test_deep_does_not_write_lesson_when_llm_returns_null(store: Store, paths, event_factory, fake_llm) -> None:
    _use_low_deep_threshold(paths)
    e = store.append(event_factory(status="abandoned", outcome="放弃", lesson=None))
    fake_llm.queue({e: None})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert store.read(e).lesson is None


def test_deep_writes_lesson_when_llm_returns_a_string(store: Store, paths, event_factory, fake_llm) -> None:
    _use_low_deep_threshold(paths)
    e = store.append(event_factory(kind="fix", status="abandoned", outcome="放弃", lesson=None))
    fake_llm.queue({e: "并行任务的端口需要按任务 id 错开分配"})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert store.read(e).lesson == "并行任务的端口需要按任务 id 错开分配"


# ==================================================================== deep：晋升／stale／水位


def test_deep_promotes_near_duplicate_lessons_via_8gram_jaccard(store: Store, paths, event_factory) -> None:
    _use_low_deep_threshold(paths)
    lesson_text = "并行启动多个Ray任务时端口需要按任务id错开分配，使用默认端口必然冲突"
    e1 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论1", lesson=lesson_text))
    e2 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论2", lesson=lesson_text))

    # client=None：蒸馏步骤整体跳过，晋升判定完全基于已经落盘的 lesson 字段
    deep(store, paths, Budget(), None, NOW)

    lessons_text = paths.lessons.read_text(encoding="utf-8")
    assert f"- [{e1}] (promoted)" in lessons_text
    assert f"- [{e2}] (promoted)" in lessons_text
    assert lesson_text in paths.working_set.read_text(encoding="utf-8")


def test_deep_retires_promoted_lesson_after_three_unreferenced_runs(store: Store, paths, event_factory) -> None:
    """DESIGN §4.5：promoted lesson 连续 3 次深整理未被引用 → retired，之后不再自动复活。"""
    _use_low_deep_threshold(paths)
    lesson_text = "并行启动多个Ray任务时端口需要按任务id错开分配，使用默认端口必然冲突"
    e1 = store.append(event_factory(kind="fix", status="abandoned", outcome="结论1", lesson=lesson_text))
    store.append(event_factory(kind="fix", status="abandoned", outcome="结论2", lesson=lesson_text))

    deep(store, paths, Budget(), None, NOW)  # 第 1 次：晋升为 promoted
    assert f"- [{e1}] (promoted)" in paths.lessons.read_text(encoding="utf-8")

    # 后续 3 次深整理都不产生任何引用该 lesson 的新事件
    for i in range(3):
        store.append(event_factory(kind="build", status="done", outcome="完成", intent=f"不相关事件{i}"))
        deep(store, paths, Budget(), None, NOW)

    assert f"- [{e1}] (retired)" in paths.lessons.read_text(encoding="utf-8")

    # 退休后不自动复活：即便再跑一次，状态仍是 retired
    store.append(event_factory(kind="build", status="done", outcome="完成", intent="又一个不相关事件"))
    deep(store, paths, Budget(), None, NOW)
    assert f"- [{e1}] (retired)" in paths.lessons.read_text(encoding="utf-8")


def test_deep_does_not_promote_a_lesson_appearing_only_once(store: Store, paths, event_factory) -> None:
    _use_low_deep_threshold(paths)
    e = store.append(event_factory(kind="fix", status="abandoned", outcome="结论", lesson="独一无二的教训文本"))

    deep(store, paths, Budget(), None, NOW)

    assert f"- [{e}] (candidate)" in paths.lessons.read_text(encoding="utf-8")


def test_deep_marks_stale_open_events_in_working_set(store: Store, paths, event_factory) -> None:
    _use_low_deep_threshold(paths)
    old_id = new_id(NOW - timedelta(days=30), set())
    store.append(event_factory(id=old_id, status="open", intent="很久之前开启但还没关闭的任务"))

    deep(store, paths, Budget(stale_days=14), None, NOW)

    open_lines = [
        line
        for line in paths.working_set.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"- [{old_id}]")
    ]
    assert len(open_lines) == 1
    assert open_lines[0].endswith("(stale)")


def test_deep_does_not_mark_recent_open_events_as_stale(store: Store, paths, event_factory) -> None:
    _use_low_deep_threshold(paths)
    recent_id = new_id(NOW - timedelta(days=1), set())
    store.append(event_factory(id=recent_id, status="open", intent="昨天才开启的任务"))

    deep(store, paths, Budget(stale_days=14), None, NOW)

    open_lines = [
        line
        for line in paths.working_set.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"- [{recent_id}]")
    ]
    assert len(open_lines) == 1
    assert not open_lines[0].endswith("(stale)")


def test_deep_advances_watermark_to_total_event_count(store: Store, paths, event_factory) -> None:
    _use_low_deep_threshold(paths)
    for _ in range(4):
        store.append(event_factory(status="abandoned", outcome="结论"))

    deep(store, paths, Budget(), None, NOW)

    assert paths.deep_watermark.read_text(encoding="utf-8").strip() == "4"
