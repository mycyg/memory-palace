"""事件粒度自适应（SPEC §3.16）：合并候选检测、粗事件检测、LLM 失败的降级、
以及 recall/index 消费方（组行浮现、整组 seen 去重、segment label 浮现）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from eventmem.consolidate import MERGE_SYSTEM, SEGMENT_SYSTEM, deep
from eventmem.index import Budget, granularity_file, load_granularity, rebuild_all
from eventmem.llm import LLMError
from eventmem.paths import atomic_write
from eventmem.recall import surface
from eventmem.schema import Anchors, new_id
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 18, 0, 0)


def _ts(minutes: int) -> str:
    """从固定基准时间起偏移若干分钟得到事件 id。"""
    base = datetime(2026, 8, 25, 10, 0, 0)
    return new_id(base + timedelta(minutes=minutes), set())


# ==================================================================== 合并候选检测（deep）


def test_merge_candidates_chain_via_adjacent_not_clique_wide_intersection(
    store: Store, paths, event_factory, fake_llm
) -> None:
    """e1∩e2 与 e2∩e3 各自非空即可成链，e1 与 e3 本身不必有交集——链式而非两两全交。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    e1 = store.append(
        event_factory(id=_ts(0), parent="2026-08-25_080000", status="done", outcome="步骤一",
                      anchors=Anchors(files=["a.py"]))
    )
    e2 = store.append(
        event_factory(id=_ts(5), parent="2026-08-25_080000", status="done", outcome="步骤二",
                      anchors=Anchors(files=["a.py", "b.py"]))
    )
    e3 = store.append(
        event_factory(id=_ts(10), parent="2026-08-25_080000", status="done", outcome="步骤三",
                      anchors=Anchors(files=["b.py"]))
    )
    fake_llm.queue({e1: "三步共同完成了某模块的调整"})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[0].system == MERGE_SYSTEM
    granularity = load_granularity(paths)
    group = granularity.group_of(e2)
    assert group is not None
    assert group.ids == (e1, e2, e3)
    assert group.summary == "三步共同完成了某模块的调整"


def test_merge_candidates_require_same_parent(store: Store, paths, event_factory, fake_llm) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(
        event_factory(id=_ts(0), parent="P", status="done", outcome="步骤一", anchors=Anchors(files=["a.py"]))
    )
    store.append(  # parent 不同，链在这里断开
        event_factory(id=_ts(5), parent=None, status="done", outcome="步骤二", anchors=Anchors(files=["a.py"]))
    )
    store.append(
        event_factory(id=_ts(10), parent="P", status="done", outcome="步骤三", anchors=Anchors(files=["a.py"]))
    )

    deep(store, paths, Budget(), fake_llm, NOW)  # 队列为空：真调用 LLM 就会 AssertionError

    assert load_granularity(paths).merged == ()


def test_merge_candidates_require_at_least_three_events(store: Store, paths, event_factory, fake_llm) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(
        event_factory(id=_ts(0), parent="P", status="done", outcome="步骤一", anchors=Anchors(files=["a.py"]))
    )
    store.append(
        event_factory(id=_ts(5), parent="P", status="done", outcome="步骤二", anchors=Anchors(files=["a.py"]))
    )

    deep(store, paths, Budget(), fake_llm, NOW)

    assert load_granularity(paths).merged == ()


def test_merge_candidates_require_gap_under_thirty_minutes(store: Store, paths, event_factory, fake_llm) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(
        event_factory(id=_ts(0), parent="P", status="done", outcome="步骤一", anchors=Anchors(files=["a.py"]))
    )
    store.append(
        event_factory(id=_ts(5), parent="P", status="done", outcome="步骤二", anchors=Anchors(files=["a.py"]))
    )
    store.append(  # 与上一条间隔 40 分钟，超过 30 分钟窗
        event_factory(id=_ts(45), parent="P", status="done", outcome="步骤三", anchors=Anchors(files=["a.py"]))
    )

    deep(store, paths, Budget(), fake_llm, NOW)

    assert load_granularity(paths).merged == ()


def test_merge_candidates_ignore_open_events(store: Store, paths, event_factory, fake_llm) -> None:
    """合并只统计已闭合事件：open 事件被整个过滤掉，不作为候选成员计数——混入一个
    open 事件后，原本凑得齐的 3 个成员只剩 2 个已闭合的，达不到 ≥3 的门槛。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(
        event_factory(id=_ts(0), parent="P", status="done", outcome="步骤一", anchors=Anchors(files=["a.py"]))
    )
    store.append(event_factory(id=_ts(5), parent="P", status="open", anchors=Anchors(files=["a.py"])))
    store.append(
        event_factory(id=_ts(10), parent="P", status="done", outcome="步骤三", anchors=Anchors(files=["a.py"]))
    )
    # 存在 open 事件会触发模型级预取的一次 LLM 调用，先喂好响应（预测为空即可，与本测试无关）
    fake_llm.queue({"predictions": []})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert load_granularity(paths).merged == ()


# ==================================================================== 粗事件检测（deep）


def test_coarse_candidate_detected_by_file_anchor_count(store: Store, paths, event_factory, fake_llm) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    files = [f"src/mod_{i}.py" for i in range(8)]
    e = store.append(event_factory(status="done", outcome="完成一次大改", anchors=Anchors(files=files)))
    fake_llm.queue({e: [{"label": "核心分段", "files": files[:4]}, {"label": "边缘分段", "files": files[4:]}]})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[0].system == SEGMENT_SYSTEM
    granularity = load_granularity(paths)
    assert granularity.segment_label(e, files[0]) == "核心分段"
    assert granularity.segment_label(e, files[4]) == "边缘分段"


def test_coarse_candidate_detected_by_wide_dialog_span_when_files_present(
    store: Store, paths, event_factory, fake_llm
) -> None:
    e = store.append(
        event_factory(
            status="done",
            outcome="完成排查",
            anchors=Anchors(files=["src/one.py"], dialog=["sess1#L100-L600"]),  # 跨度 500 > 400
        )
    )
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    fake_llm.queue({e: [{"label": "唯一分段", "files": ["src/one.py"]}]})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert load_granularity(paths).segment_label(e, "src/one.py") == "唯一分段"


def test_coarse_candidate_with_wide_dialog_span_but_no_files_is_excluded(
    store: Store, paths, event_factory, fake_llm
) -> None:
    """无文件锚点则无从分段（docstring 明文）：仅靠 dialog 跨度不足以成为候选。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(
        event_factory(status="done", outcome="完成排查", anchors=Anchors(dialog=["sess1#L100-L600"]))
    )

    deep(store, paths, Budget(), fake_llm, NOW)  # 队列为空：真调用 LLM 就会 AssertionError

    assert load_granularity(paths).coarse == {}


def test_segment_files_not_in_event_anchors_are_discarded(store: Store, paths, event_factory, fake_llm) -> None:
    """模型编造的文件不在事件自身的锚点列表内，应被丢弃。"""
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    files = [f"src/mod_{i}.py" for i in range(8)]
    e = store.append(event_factory(status="done", outcome="完成一次大改", anchors=Anchors(files=files)))
    fake_llm.queue({e: [{"label": "含幻觉文件的分段", "files": [files[0], "src/invented_ghost.py"]}]})

    deep(store, paths, Budget(), fake_llm, NOW)

    granularity = load_granularity(paths)
    assert granularity.segment_label(e, files[0]) == "含幻觉文件的分段"
    assert granularity.segment_label(e, "src/invented_ghost.py") is None


# ==================================================================== LLM 失败保留上轮视图


def test_granularity_preserves_previous_view_when_llm_fails_on_both_passes(
    store: Store, paths, event_factory, fake_llm
) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    e1 = store.append(
        event_factory(id=_ts(0), parent="P", status="done", outcome="步骤一", anchors=Anchors(files=["a.py"]))
    )
    e2 = store.append(
        event_factory(id=_ts(5), parent="P", status="done", outcome="步骤二", anchors=Anchors(files=["a.py", "b.py"]))
    )
    e3 = store.append(
        event_factory(id=_ts(10), parent="P", status="done", outcome="步骤三", anchors=Anchors(files=["b.py"]))
    )
    coarse_files = [f"src/mod_{i}.py" for i in range(8)]
    e4 = store.append(event_factory(status="done", outcome="完成一次大改", anchors=Anchors(files=coarse_files)))

    fake_llm.queue(
        {e1: "三步共同完成了某模块的调整"},  # 第一轮：合并概括成功
        {e4: [{"label": "核心分段", "files": coarse_files}]},  # 第一轮：分段成功
        LLMError("网络故障"),  # 第二轮：合并概括失败
        LLMError("网络故障"),  # 第二轮：分段也失败
    )

    deep(store, paths, Budget(), fake_llm, NOW)
    first_round_bytes = granularity_file(paths).read_bytes()
    assert load_granularity(paths).group_of(e2) is not None  # 第一轮确实生效了

    # 追加一个不相关事件推高脏量，让第二轮深整理真正执行到 _detect_granularity
    store.append(event_factory(status="done", outcome="无关事件", intent="无关意图"))
    deep(store, paths, Budget(), fake_llm, NOW)

    assert granularity_file(paths).read_bytes() == first_round_bytes  # 两次 LLM 都失败，视图原样保留
    granularity = load_granularity(paths)
    assert granularity.group_of(e2) is not None
    assert granularity.segment_label(e4, coarse_files[0]) == "核心分段"


# ==================================================================== recall.surface：组行浮现与 seen 去重


def _write_granularity(paths, merged=(), coarse=()) -> None:
    payload = {"merged": list(merged), "coarse": list(coarse)}
    atomic_write(granularity_file(paths), json.dumps(payload, ensure_ascii=False))


def test_surface_folds_group_members_into_a_single_group_row(store: Store, paths, event_factory) -> None:
    e1 = store.append(event_factory(status="done", outcome="步骤一"))
    e2 = store.append(event_factory(status="done", outcome="步骤二", anchors=Anchors(files=["shared.py"])))
    e3 = store.append(event_factory(status="done", outcome="步骤三"))
    rebuild_all(store, paths, Budget(), NOW)
    _write_granularity(
        paths,
        merged=[{"ids": [e1, e2, e3], "summary": "三步共同完成了某模块的调整", "anchors_union": ["file:shared.py"]}],
    )

    hits = surface("shared.py", "file", store, paths, Budget(), seen=set())

    assert len(hits) == 1
    assert hits[0].event_id == e1  # 组的稳定标识是首成员 id
    assert hits[0].line == f"[{e1}+2] 三步共同完成了某模块的调整"


def test_surface_deduplicates_whole_group_when_any_member_is_seen(store: Store, paths, event_factory) -> None:
    e1 = store.append(event_factory(status="done", outcome="步骤一"))
    e2 = store.append(event_factory(status="done", outcome="步骤二", anchors=Anchors(files=["shared.py"])))
    e3 = store.append(event_factory(status="done", outcome="步骤三"))
    rebuild_all(store, paths, Budget(), NOW)
    _write_granularity(paths, merged=[{"ids": [e1, e2, e3], "summary": "组概括", "anchors_union": []}])

    # seen 里只有 e3（组内既非首成员也非命中锚点的那个），整组仍应被去重
    hits = surface("shared.py", "file", store, paths, Budget(), seen={e3})

    assert hits == []


# ==================================================================== recall.surface：segment label 浮现


def test_surface_shows_segment_label_instead_of_outcome_for_coarse_event(
    store: Store, paths, event_factory
) -> None:
    e = store.append(
        event_factory(status="done", outcome="完成一次大改", anchors=Anchors(files=["src/export.py", "src/other.py"]))
    )
    rebuild_all(store, paths, Budget(), NOW)
    _write_granularity(
        paths,
        coarse=[
            {
                "id": e,
                "segments": [
                    {"label": "导出模块的分页写出", "files": ["src/export.py"]},
                    {"label": "其他辅助改动", "files": ["src/other.py"]},
                ],
            }
        ],
    )

    hits = surface("src/export.py", "file", store, paths, Budget(), seen=set())

    assert len(hits) == 1
    assert hits[0].line == f"[{e}] 导出模块的分页写出"
    assert "完成一次大改" not in hits[0].line


def test_surface_falls_back_to_outcome_for_error_cue_even_with_coarse_segments(
    store: Store, paths, event_factory
) -> None:
    """segment label 只在 file 线索下生效（cue_file 仅 kind=="file" 时非空）。"""
    from eventmem.recall import error_signature

    sig = error_signature("ValueError: port busy")
    e = store.append(
        event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/export.py"], error_sigs=[sig]))
    )
    rebuild_all(store, paths, Budget(), NOW)
    _write_granularity(paths, coarse=[{"id": e, "segments": [{"label": "分段标题", "files": ["src/export.py"]}]}])

    hits = surface("ValueError: port busy", "error", store, paths, Budget(), seen=set())

    assert hits[0].line == f"[{e}] 端口冲突已修复"


# ==================================================================== granularity.json 缺失退回 v0.1 行为


def test_load_granularity_returns_empty_when_file_missing(paths) -> None:
    assert not granularity_file(paths).exists()
    granularity = load_granularity(paths)
    assert granularity.is_empty()
    assert granularity.group_of("any-id") is None
    assert granularity.segment_label("any-id", "any.py") is None


def test_surface_shows_individual_hits_when_granularity_missing(store: Store, paths, event_factory) -> None:
    ids = [
        store.append(event_factory(status="done", outcome=f"结论{i}", anchors=Anchors(files=["shared.py"])))
        for i in range(3)
    ]
    rebuild_all(store, paths, Budget(), NOW)

    hits = surface("shared.py", "file", store, paths, Budget(surface_k=3), seen=set())

    assert {h.event_id for h in hits} == set(ids)  # 逐事件展示，未被折叠成组行
