"""store.py：L0 append-only 存储的生命周期与不可变纪律。"""

from __future__ import annotations

import pytest

from eventmem.schema import Anchors
from eventmem.store import AlreadyClosed, EventNotFound, Store

# ---------------------------------------------------------------- append / read


def test_append_then_read_round_trips(store: Store, event_factory) -> None:
    e = event_factory(intent="写入并读回")
    final_id = store.append(e)
    assert final_id == e.id
    assert store.read(final_id) == e


def test_append_id_conflict_creates_suffixed_copy(store: Store, event_factory) -> None:
    e1 = event_factory(id="2026-08-25_120000", intent="第一个")
    e2 = event_factory(id="2026-08-25_120000", intent="第二个-同id")
    id1 = store.append(e1)
    id2 = store.append(e2)

    assert id1 == "2026-08-25_120000"
    assert id2 == "2026-08-25_120000-2"
    assert store.read(id1).intent == "第一个"
    assert store.read(id2).intent == "第二个-同id"


def test_append_does_not_mutate_caller_event_object(store: Store, event_factory) -> None:
    e1 = event_factory(id="2026-08-25_120000")
    e2 = event_factory(id="2026-08-25_120000")
    store.append(e1)
    store.append(e2)
    assert e2.id == "2026-08-25_120000"  # append 返回新 id，但不改写传入对象


def test_read_missing_event_raises_event_not_found(store: Store) -> None:
    with pytest.raises(EventNotFound):
        store.read("2026-01-01_000000")


def test_all_ids_sorted_ascending(store: Store, event_factory) -> None:
    for eid in ("2026-08-25_120200", "2026-08-25_120000", "2026-08-25_120100"):
        store.append(event_factory(id=eid))
    assert store.all_ids() == ["2026-08-25_120000", "2026-08-25_120100", "2026-08-25_120200"]


def test_exists_reflects_appended_events(store: Store, event_factory) -> None:
    e = event_factory(id="2026-08-25_130000")
    assert not store.exists(e.id)
    store.append(e)
    assert store.exists(e.id)


# ---------------------------------------------------------------- close 生命周期


def test_close_open_event_sets_status_and_outcome(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.close(e, "done", "完成了")
    closed = store.read(e)
    assert closed.status == "done"
    assert closed.outcome == "完成了"


@pytest.mark.parametrize("target_status", ["done", "abandoned"])
def test_close_accepts_all_terminal_statuses(store: Store, event_factory, target_status: str) -> None:
    e = store.append(event_factory(status="open"))
    store.close(e, target_status, "结束")
    assert store.read(e).status == target_status


def test_close_twice_raises_already_closed(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.close(e, "done", "第一次闭合")
    with pytest.raises(AlreadyClosed):
        store.close(e, "abandoned", "第二次闭合")
    # 重复 close 失败后原有 outcome 不应被改动
    assert store.read(e).outcome == "第一次闭合"


def test_close_rejects_target_status_open(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    with pytest.raises(ValueError):
        store.close(e, "open", "不合法的目标状态")


def test_close_missing_event_raises_event_not_found(store: Store) -> None:
    with pytest.raises(EventNotFound):
        store.close("2026-01-01_000000", "done", "x")


# ---------------------------------------------------------------- add_anchors


def test_add_anchors_merges_as_union(store: Store, event_factory) -> None:
    e = store.append(event_factory(anchors=Anchors(files=["a.py"])))
    store.add_anchors(e, Anchors(files=["b.py"], commits=["a3f21c9"]))
    merged = store.read(e).anchors
    assert merged.files == ["a.py", "b.py"]
    assert merged.commits == ["a3f21c9"]


def test_add_anchors_is_idempotent(store: Store, event_factory) -> None:
    e = store.append(event_factory(anchors=Anchors(files=["a.py"])))
    anchors = Anchors(files=["a.py", "b.py"], error_sigs=["ValueError: x"])
    store.add_anchors(e, anchors)
    store.add_anchors(e, anchors)
    result = store.read(e).anchors
    assert result.files == ["a.py", "b.py"]
    assert result.error_sigs == ["ValueError: x"]


def test_add_anchors_preserves_existing_order(store: Store, event_factory) -> None:
    e = store.append(event_factory(anchors=Anchors(files=["z.py", "a.py"])))
    store.add_anchors(e, Anchors(files=["a.py", "m.py"]))
    assert store.read(e).anchors.files == ["z.py", "a.py", "m.py"]


# ---------------------------------------------------------------- set_lesson


def test_set_lesson_writes_new_value(store: Store, event_factory) -> None:
    e = store.append(event_factory(lesson=None))
    store.set_lesson(e, "并行任务的端口需要错开分配")
    assert store.read(e).lesson == "并行任务的端口需要错开分配"


def test_set_lesson_overwrites_previous_value(store: Store, event_factory) -> None:
    e = store.append(event_factory(lesson="旧教训"))
    store.set_lesson(e, "重蒸馏后的新教训")
    assert store.read(e).lesson == "重蒸馏后的新教训"


# ---------------------------------------------------------------- set_outcome 三分支


def test_set_outcome_raises_when_event_still_open(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open", outcome=None))
    with pytest.raises(AlreadyClosed):
        store.set_outcome(e, "尝试在 open 时补写")
    assert store.read(e).outcome is None


def test_set_outcome_raises_when_outcome_already_present(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome="已有结论"))
    with pytest.raises(AlreadyClosed):
        store.set_outcome(e, "试图覆盖")
    assert store.read(e).outcome == "已有结论"


def test_set_outcome_fills_missing_outcome_on_closed_event(store: Store, event_factory) -> None:
    """轻整理的补全通道：已闭合但 outcome 为空 → 合法补写。"""
    e = store.append(event_factory(status="abandoned", outcome=None))
    store.set_outcome(e, "补写的结论")
    result = store.read(e)
    assert result.outcome == "补写的结论"
    assert result.status == "abandoned"  # 补写不改动 status


def test_set_outcome_treats_empty_string_outcome_as_missing(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome=""))
    store.set_outcome(e, "补写的结论")
    assert store.read(e).outcome == "补写的结论"


# ---------------------------------------------------------------- mark_superseded


def test_mark_superseded_transitions_status(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.mark_superseded(e, "2026-08-26_090000")
    result = store.read(e)
    assert result.status == "superseded"
    assert result.superseded_by == "2026-08-26_090000"


def test_mark_superseded_is_idempotent_with_same_target(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.mark_superseded(e, "2026-08-26_090000")
    store.mark_superseded(e, "2026-08-26_090000")  # 重复调用同一目标，不应报错
    result = store.read(e)
    assert result.status == "superseded"
    assert result.superseded_by == "2026-08-26_090000"


def test_mark_superseded_raises_when_repointed_to_different_target(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.mark_superseded(e, "2026-08-26_090000")
    with pytest.raises(AlreadyClosed):
        store.mark_superseded(e, "2026-08-27_000000")
    # 改指失败后原指向不应改变
    assert store.read(e).superseded_by == "2026-08-26_090000"


# 裁决（集成阶段）：close 与 mark_superseded 两条路径合一——close 只接受 done/abandoned，
# 推翻一律走 mark_superseded（那里才有 by 链接），因此不存在「close 出无链接 superseded」的状态。
def test_close_rejects_superseded_as_target_status(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    with pytest.raises(ValueError):
        store.close(e, "superseded", "试图经 close() 推翻")
    assert store.read(e).status == "open"  # 拒绝后原状态不变


# 裁决（集成阶段）：done → superseded 是核心语义——已完成的决策可以被事后推翻
# （DESIGN §2.4 状态机随之补充「任意状态 → superseded」这条边），不是需要拒绝的误用。
def test_mark_superseded_on_done_event_is_legal_late_overrule(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    store.close(e, "done", "已完成的结论")
    store.mark_superseded(e, "2026-08-26_000000")
    r = store.read(e)
    assert r.status == "superseded"
    assert r.superseded_by == "2026-08-26_000000"
    assert r.outcome == "已完成的结论"  # 推翻不抹结论：当时的结果仍是历史事实


# ---------------------------------------------------------------- iter_events


def test_iter_events_yields_in_ascending_id_order(store: Store, event_factory) -> None:
    for eid in ("2026-08-25_120200", "2026-08-25_120000", "2026-08-25_120100"):
        store.append(event_factory(id=eid))
    assert [e.id for e in store.iter_events()] == [
        "2026-08-25_120000",
        "2026-08-25_120100",
        "2026-08-25_120200",
    ]


def test_iter_events_skips_corrupt_files_without_raising(store: Store, event_factory) -> None:
    good1 = store.append(event_factory(id="2026-08-25_120000"))
    good2 = store.append(event_factory(id="2026-08-25_120200"))
    # 直接在磁盘上放一个无法解析的坏文件（既不合法 frontmatter，也不是合法 yaml 结构）
    bad_path = store.paths.event_file("2026-08-25_120100")
    bad_path.write_text("这不是一个合法的事件文件", encoding="utf-8")

    assert "2026-08-25_120100" in store.all_ids()  # all_ids 只看文件名，不解析
    ids = [e.id for e in store.iter_events()]
    assert ids == [good1, good2]  # iter_events 跳过解析失败的文件，不中断


# ---------------------------------------------------------------- 不可变纪律


def test_store_public_api_has_no_intent_or_body_mutators() -> None:
    """不可变纪律的代码表达：Store 公开方法名单里不存在修改 intent/body 的接口。

    用精确名单而非关键字黑名单断言：新增任何公开方法都会让这条测试失败，
    逼迫改动者回到这里确认新方法没有绕开不可变纪律。
    """
    public_members = sorted(name for name in dir(Store) if not name.startswith("_"))
    expected = sorted(
        [
            "add_anchors",
            "all_ids",
            "append",
            "close",
            "exists",
            "iter_events",
            "mark_superseded",
            "paths",
            "read",
            "set_lesson",
            "set_outcome",
            "set_salience_prior",  # v0.2：只写 salience_prior/salience_reason，仍走 _rewrite 护栏
        ]
    )
    assert public_members == expected

    forbidden_fragments = ("intent", "body")
    for name in public_members:
        if name in ("paths",):
            continue
        for fragment in forbidden_fragments:
            assert fragment not in name.lower(), f"疑似违反不可变纪律的公开方法：{name}"


def test_rewrite_guard_rejects_intent_or_body_mutation_even_internally(store: Store, event_factory) -> None:
    """防御性回归：不可变纪律不只靠「不提供方法」，_rewrite 内部还有二次校验。

    公开方法目前都不会改 intent/body，这条测试直接调用私有的 _rewrite 钩子，
    确认即使未来某个 mutate 闭包写错、试图改 intent，也会被 RuntimeError 拦下
    而不是静默写入。
    """
    from dataclasses import replace

    e = store.append(event_factory(intent="原始意图", body="原始正文"))

    def mutate_intent(ev):
        return replace(ev, intent="被篡改的意图")

    with pytest.raises(RuntimeError):
        store._rewrite(e, mutate_intent)  # noqa: SLF001 —— 故意触达内部护栏
    assert store.read(e).intent == "原始意图"

    def mutate_body(ev):
        return replace(ev, body="被篡改的正文")

    with pytest.raises(RuntimeError):
        store._rewrite(e, mutate_body)  # noqa: SLF001
    assert store.read(e).body == "原始正文"
