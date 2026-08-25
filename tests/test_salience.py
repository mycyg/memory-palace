"""显著性（salience，SPEC §3.11）：schema 扩展字段、store.set_salience_prior、
consolidate 的后验公式与 clamp、recall.surface 与 working-set 的排序消费。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from eventmem.consolidate import (
    PRIOR_SYSTEM,
    kind_default_prior,
    light,
    salience_score,
)
from eventmem.index import Budget, rebuild_all
from eventmem.paths import atomic_write
from eventmem.recall import surface
from eventmem.schema import Anchors, from_markdown, to_markdown
from eventmem.store import AlreadyClosed, Store

NOW = datetime(2026, 8, 25, 14, 32, 1)


def _write_salience(paths, scores: dict[str, float], **evidence_by_id) -> None:
    """写一份最小 salience.json：{id: score}，evidence 缺省全零。"""
    import json

    payload = {
        event_id: {
            "score": score,
            "prior": "medium",
            "evidence": evidence_by_id.get(
                event_id, {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False}
            ),
            "updated": NOW.isoformat(timespec="seconds"),
        }
        for event_id, score in scores.items()
    }
    atomic_write(paths.index_dir / "salience.json", json.dumps(payload, ensure_ascii=False))


# ==================================================================== schema：字段往返与旧文件兼容


def test_salience_fields_round_trip(event_factory) -> None:
    e = event_factory(
        status="done",
        outcome="完成",
        salience_prior="high",
        salience_reason="记录了被否决的方案与否决理由",
    )
    assert from_markdown(to_markdown(e)) == e


def test_prospective_field_round_trips(event_factory) -> None:
    e = event_factory(status="open", intent="下次：给 launcher 补一个端口占用的预检查", prospective=True)
    assert from_markdown(to_markdown(e)) == e
    assert from_markdown(to_markdown(e)).prospective is True


def test_v02_fields_at_default_are_omitted_from_frontmatter(event_factory) -> None:
    """取默认值时不写进 frontmatter：旧事件重写后 diff 保持最小（docstring 明文承诺）。"""
    e = event_factory(status="open", salience_prior=None, salience_reason=None, prospective=False)
    md = to_markdown(e)
    assert "salience_prior" not in md
    assert "salience_reason" not in md
    assert "prospective" not in md


def test_old_event_file_without_v02_fields_parses_to_defaults() -> None:
    """v0.1 时代写下的事件文件（完全没有这三个字段）必须能正常解析。"""
    raw = "---\nid: a\nkind: fix\nstatus: open\nintent: x\n---\n"
    e = from_markdown(raw)
    assert e.salience_prior is None
    assert e.salience_reason is None
    assert e.prospective is False


def test_salience_prior_illegal_value_is_treated_as_missing_not_an_error() -> None:
    raw = "---\nid: a\nkind: fix\nstatus: done\nintent: x\nsalience_prior: extreme\n---\n"
    e = from_markdown(raw)  # 不应 raise
    assert e.salience_prior is None


def test_prospective_field_accepts_yaml_and_string_truthy_forms() -> None:
    assert from_markdown("---\nid: a\nkind: build\nstatus: open\nintent: x\nprospective: true\n---\n").prospective
    assert from_markdown("---\nid: a\nkind: build\nstatus: open\nintent: x\nprospective: 'yes'\n---\n").prospective
    assert not from_markdown("---\nid: a\nkind: build\nstatus: open\nintent: x\nprospective: 'no'\n---\n").prospective
    assert not from_markdown("---\nid: a\nkind: build\nstatus: open\nintent: x\n---\n").prospective


# ==================================================================== store.set_salience_prior 三分支


def test_set_salience_prior_rejects_open_event(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="open"))
    with pytest.raises(AlreadyClosed):
        store.set_salience_prior(e, "high", "尝试在 open 时补评")
    assert store.read(e).salience_prior is None


def test_set_salience_prior_rejects_when_already_present(store: Store, event_factory) -> None:
    e = store.append(
        event_factory(status="done", outcome="完成", salience_prior="low", salience_reason="旧理由")
    )
    with pytest.raises(AlreadyClosed):
        store.set_salience_prior(e, "high", "试图覆盖")
    result = store.read(e)
    assert result.salience_prior == "low"
    assert result.salience_reason == "旧理由"


def test_set_salience_prior_writes_on_closed_event_without_prior(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="abandoned", outcome="放弃", salience_prior=None))
    store.set_salience_prior(e, "medium", "缓存穿透的成因可复用")
    result = store.read(e)
    assert result.salience_prior == "medium"
    assert result.salience_reason == "缓存穿透的成因可复用"


def test_set_salience_prior_rejects_illegal_prior_value(store: Store, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome="完成", salience_prior=None))
    with pytest.raises(ValueError):
        store.set_salience_prior(e, "extreme", "非法档位")  # type: ignore[arg-type]
    assert store.read(e).salience_prior is None


def test_set_salience_prior_does_not_touch_intent_or_body(store: Store, event_factory) -> None:
    e = store.append(
        event_factory(status="done", outcome="完成", intent="原始意图", body="原始正文", salience_prior=None)
    )
    store.set_salience_prior(e, "high", "理由")
    result = store.read(e)
    assert result.intent == "原始意图"
    assert result.body == "原始正文"


# ==================================================================== consolidate.kind_default_prior


@pytest.mark.parametrize(
    "kind,status,expected",
    [
        ("decision", "done", "high"),
        ("decision", "abandoned", "high"),
        ("fix", "done", "medium"),
        ("fix", "abandoned", "medium"),
        ("explore", "abandoned", "medium"),
        ("explore", "done", "low"),  # 表里未列出的组合拿不准归低档
        ("build", "done", "low"),
        ("build", "open", "low"),
    ],
)
def test_kind_default_prior_table(kind: str, status: str, expected: str) -> None:
    assert kind_default_prior(kind, status) == expected


# ==================================================================== consolidate.salience_score 公式与 clamp


def test_salience_score_decision_floor_lifts_a_low_raw_score(event_factory) -> None:
    e = event_factory(kind="decision", status="done")
    evidence = {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False}
    # raw = 0.35*0.2 = 0.07，低于 DECISION_FLOOR=0.4，应被拉到 0.4
    assert salience_score(e, "low", evidence) == pytest.approx(0.4)


def test_salience_score_decision_floor_does_not_reduce_an_already_higher_score(event_factory) -> None:
    e = event_factory(kind="decision", status="done")
    evidence = {"refs": 0, "hits": 1, "ignored": 0, "superseded_trigger": False}
    # raw = 0.35*0.8 + 0.30*(1/1) = 0.28 + 0.30 = 0.58，高于下限，floor 不应把它拉低
    assert salience_score(e, "high", evidence) == pytest.approx(0.58)


def test_salience_score_smooth_build_cap_binds_with_zero_evidence(event_factory) -> None:
    """集成裁决：cap 调至 0.25，使其在零证据时真实咬合——高自评（prior 项 0.28）的
    顺利 build 被压到 0.25，不再是死分支。"""
    e = event_factory(kind="build", status="done", anchors=Anchors())  # 无 error_sigs
    evidence = {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False}
    assert salience_score(e, "high", evidence) == pytest.approx(0.25)


def test_salience_score_smooth_build_cap_is_ineffective_once_evidence_lifts_it(event_factory) -> None:
    """SPEC：「顺利 build 上限 0.5（有 evidence 抬升时失效）」——一旦 hits/refs/supersede
    任一证据非零（lifted=True），返回值不再被压到 0.5，即便远高于它。"""
    e = event_factory(kind="build", status="done", anchors=Anchors())
    evidence = {"refs": 4, "hits": 3, "ignored": 0, "superseded_trigger": True}
    # raw = 0.28 + 0.25*1 + 0.30*1 + 0 + 0.20 = 1.03 -> 全局 clamp 到 1.0，且不再被 0.5 封顶
    assert salience_score(e, "high", evidence) == pytest.approx(1.0)


def test_salience_score_smooth_build_requires_done_status_and_no_error_sigs(event_factory) -> None:
    """open 状态或带 error_sigs 的 build 事件不算「顺利」，不进入封顶分支：
    保持 prior 项原值 0.28，与被封顶的顺利 build（0.25）形成真实区分。"""
    open_build = event_factory(kind="build", status="open", anchors=Anchors())
    with_error = event_factory(kind="build", status="done", anchors=Anchors(error_sigs=["x"]))
    evidence = {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False}
    assert salience_score(open_build, "high", evidence) == pytest.approx(0.28)
    assert salience_score(with_error, "high", evidence) == pytest.approx(0.28)


def test_salience_score_global_clamp_keeps_result_within_zero_and_one(event_factory) -> None:
    e = event_factory(kind="fix", status="done")
    # 大量正向证据也不应超过 1.0
    high_evidence = {"refs": 40, "hits": 40, "ignored": 0, "superseded_trigger": True}
    assert salience_score(e, "high", high_evidence) <= 1.0
    # 大量 ignored 且无 hits 时不应低于 0.0
    low_evidence = {"refs": 0, "hits": 0, "ignored": 40, "superseded_trigger": False}
    assert salience_score(e, "low", low_evidence) >= 0.0


# （原 xfail「cap=0.5 为死分支」已按集成裁决修复：cap 调至 0.25，正向断言并入上方
# test_salience_score_smooth_build_cap_binds_with_zero_evidence。）


def test_salience_score_ignored_reduces_score_without_counting_as_lifted(event_factory) -> None:
    """ignored 证据本身拉低分数，但不满足 lifted 条件（lifted 只看 hits/refs/trigger）。"""
    e = event_factory(kind="fix", status="done")
    baseline = salience_score(e, "medium", {"refs": 0, "hits": 0, "ignored": 0, "superseded_trigger": False})
    with_ignored = salience_score(e, "medium", {"refs": 0, "hits": 0, "ignored": 4, "superseded_trigger": False})
    assert with_ignored < baseline


# ==================================================================== light：先验补评


def test_light_backfills_prior_using_kind_default_table_without_client(store: Store, paths, event_factory) -> None:
    e = store.append(event_factory(kind="decision", status="done", outcome="选定方案 A", salience_prior=None))
    light(store, paths, Budget(), None, NOW)
    result = store.read(e)
    assert result.salience_prior == "high"
    assert result.salience_reason == "按 kind 规则表默认"


def test_light_backfills_prior_via_llm_when_client_present(store: Store, paths, event_factory, fake_llm) -> None:
    e = store.append(event_factory(kind="build", status="done", outcome="已有的结论", salience_prior=None))
    fake_llm.queue({e: {"prior": "high", "reason": "这是模型给出的理由"}})

    light(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[0].system == PRIOR_SYSTEM
    result = store.read(e)
    assert result.salience_prior == "high"
    assert result.salience_reason == "这是模型给出的理由"


def test_light_prior_backfill_falls_back_to_kind_default_when_llm_fails(
    store: Store, paths, event_factory, fake_llm
) -> None:
    from eventmem.llm import LLMError

    e = store.append(event_factory(kind="fix", status="done", outcome="已修复", salience_prior=None))
    fake_llm.queue(LLMError("模拟网络失败"))

    light(store, paths, Budget(), fake_llm, NOW)

    result = store.read(e)
    assert result.salience_prior == "medium"  # fix 的规则表默认
    assert result.salience_reason == "按 kind 规则表默认"


def test_light_does_not_backfill_open_events_or_events_with_existing_prior(
    store: Store, paths, event_factory
) -> None:
    open_event = store.append(event_factory(status="open", salience_prior=None))
    already_priored = store.append(
        event_factory(status="done", outcome="完成", salience_prior="low", salience_reason="人工先给的理由")
    )

    light(store, paths, Budget(), None, NOW)

    assert store.read(open_event).salience_prior is None
    result = store.read(already_priored)
    assert result.salience_prior == "low"
    assert result.salience_reason == "人工先给的理由"


# ==================================================================== recall.surface：salience 排序


def test_surface_orders_by_salience_score_overriding_status_weight(store: Store, paths, event_factory) -> None:
    """salience 是第一排序键：即便 open 状态权重天然更低，显著性够高也能排到前面。"""
    high_salience_open = store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"])))
    low_salience_done = store.append(
        event_factory(status="done", outcome="完成", anchors=Anchors(files=["shared.py"]))
    )
    rebuild_all(store, paths, Budget(), NOW)
    _write_salience(paths, {high_salience_open: 0.9, low_salience_done: 0.1})

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=2), seen=set())

    assert [h.event_id for h in hits] == [high_salience_open, low_salience_done]


def test_surface_missing_salience_json_falls_back_to_status_weight(store: Store, paths, event_factory) -> None:
    assert not (paths.index_dir / "salience.json").exists()
    e_open = store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"])))
    e_done = store.append(event_factory(status="done", outcome="完成", anchors=Anchors(files=["shared.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=2), seen=set())

    assert [h.event_id for h in hits] == [e_done, e_open]  # done 状态权重高于 open


def test_surface_ranks_anchor_overlap_count_before_recency_on_salience_tie(
    store: Store, paths, event_factory
) -> None:
    """双锚点命中场景：intent 线索展开成多个 token key，重合数更高的候选即便更旧
    也应排在重合数更低的更新候选之前——重合数排在新近度前才可能生效。"""
    older_more_overlap = store.append(
        event_factory(status="done", outcome="结论A", intent="端口冲突")
    )
    newer_less_overlap = store.append(
        event_factory(status="done", outcome="结论B", intent="认真排查故障原因")
    )
    rebuild_all(store, paths, Budget(), NOW)
    # 显著性打平，逼迫排序落到（重合数，新近度）这一级
    _write_salience(paths, {older_more_overlap: 0.5, newer_less_overlap: 0.5})

    hits = surface("端口冲突排查", "intent", store, paths, Budget(surface_k=2), seen=set())

    assert [h.event_id for h in hits] == [older_more_overlap, newer_less_overlap]


# ==================================================================== working-set：Recent outcomes 按 salience 排序


def test_working_set_recent_outcomes_ordered_by_salience_not_recency(store: Store, paths, event_factory) -> None:
    older_high = store.append(event_factory(status="done", outcome="旧但重要的结论"))
    newer_low = store.append(event_factory(status="done", outcome="新但不重要的结论"))
    rebuild_all(store, paths, Budget(), NOW)  # 先建好基础索引
    _write_salience(paths, {older_high: 0.9, newer_low: 0.1})

    rebuild_all(store, paths, Budget(), NOW)  # 重建以读取刚写入的 salience.json

    ws = paths.working_set.read_text(encoding="utf-8")
    outcomes_section = ws.split("## Recent outcomes", 1)[1].split("## Lessons", 1)[0]
    assert outcomes_section.index("旧但重要的结论") < outcomes_section.index("新但不重要的结论")
