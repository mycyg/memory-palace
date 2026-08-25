"""预取（预测 pass，SPEC §3.12）：前瞻标记捕获、规则级／模型级预取、working-set
的 Likely next 区渲染与预算、prefetch-outcome.jsonl 命中记录。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eventmem.consolidate import PREFETCH_SYSTEM, deep, light
from eventmem.extract import extract_events
from eventmem.index import Budget, load_prefetch, prefetch_file, rebuild_all
from eventmem.paths import atomic_write
from eventmem.schema import Anchors
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _user_text(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


# ==================================================================== 前瞻标记事件（extract 层）


def test_llm_phase_marks_prospective_event_forcing_kind_status_and_prefix(
    store: Store, paths, tmp_path: Path, fake_llm
) -> None:
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript_path, [_user_text("今天先到这，下次记得处理这个"), _assistant_text("好的，记下了")]
    )
    fake_llm.queue(
        {
            "events": [
                {
                    "prospective": True,
                    "intent": "给launcher补一个预检查",
                    # 前瞻标记的三个字段由 SPEC 定死，模型即便给出下列字段也应被忽略
                    "kind": "decision",
                    "status": "done",
                    "outcome": "某个不应该被采用的结论",
                    "salience_prior": "high",
                }
            ]
        }
    )

    created = extract_events(transcript_path, store, fake_llm, "sess-prospective", NOW)

    assert len(created) == 1
    event = store.read(created[0])
    assert event.prospective is True
    assert event.kind == "build"
    assert event.status == "open"
    assert event.intent == "下次：给launcher补一个预检查"
    assert event.outcome is None
    assert event.salience_prior is None


def test_llm_phase_does_not_double_prefix_already_prefixed_prospective_intent(
    store: Store, paths, tmp_path: Path, fake_llm
) -> None:
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, [_user_text("先聊到这"), _assistant_text("好")])
    fake_llm.queue({"events": [{"prospective": True, "intent": "下次：补充测试覆盖率"}]})

    created = extract_events(transcript_path, store, fake_llm, "sess-prefixed", NOW)

    assert store.read(created[0]).intent == "下次：补充测试覆盖率"


# ==================================================================== light：规则级预取


def test_light_rule_level_prefetch_links_open_event_file_to_closed_history(
    store: Store, paths, event_factory
) -> None:
    open_event = store.append(
        event_factory(status="open", intent="继续处理导出模块", anchors=Anchors(files=["src/export.py"]))
    )
    closed_event = store.append(
        event_factory(status="done", outcome="修复了分页 bug", anchors=Anchors(files=["src/export.py"]))
    )

    light(store, paths, Budget(), None, NOW)

    prefetch = load_prefetch(paths)
    assert len(prefetch["items"]) == 1
    item = prefetch["items"][0]
    assert item["event_id"] == closed_event
    assert item["source"] == "rule"
    assert item["anchor"] == "src/export.py"
    assert item["text"] == "修复了分页 bug"
    assert "file:src/export.py" in prefetch["anchors"]
    assert open_event != closed_event  # 仅用于消除未使用变量的告警


def test_light_rule_prefetch_never_calls_model_level(store: Store, paths, event_factory, fake_llm) -> None:
    """light 只做规则级预取（SPEC §3.12）：即便传了 client，也不应触发模型级调用。"""
    store.append(event_factory(status="open", anchors=Anchors(files=["a.py"])))
    store.append(event_factory(status="done", outcome="结论", anchors=Anchors(files=["a.py"])))
    # 已闭合事件缺 salience_prior，会触发一次先验补评的 LLM 调用，先喂好避免队列为空报错
    fake_llm.queue({})

    light(store, paths, Budget(), fake_llm, NOW)

    assert all(call.system != PREFETCH_SYSTEM for call in fake_llm.calls)


def test_light_rule_prefetch_ignores_events_that_are_still_open(store: Store, paths, event_factory) -> None:
    """候选必须是已闭合事件；两个 open 事件共享锚点不该互相预取。"""
    store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"]), intent="任务一"))
    store.append(event_factory(status="open", anchors=Anchors(files=["shared.py"]), intent="任务二"))

    light(store, paths, Budget(), None, NOW)

    assert load_prefetch(paths)["items"] == []


# ==================================================================== deep：模型级预取


def test_deep_model_level_prefetch_uses_llm_predictions_and_filters_hallucinated_anchors(
    store: Store, paths, event_factory, fake_llm
) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    closed_event = store.append(
        event_factory(status="done", outcome="切换到 JWT 认证方案", anchors=Anchors(files=["src/auth.py"]))
    )
    store.append(event_factory(status="open", intent="继续完善认证模块的测试覆盖"))
    fake_llm.queue(
        {"predictions": [{"text": "继续完善认证模块", "anchors": ["src/auth.py", "src/ghost_auth.py"]}]}
    )

    deep(store, paths, Budget(), fake_llm, NOW)

    assert fake_llm.calls[0].system == PREFETCH_SYSTEM
    prefetch = load_prefetch(paths)
    assert len(prefetch["items"]) == 1
    item = prefetch["items"][0]
    assert item["event_id"] == closed_event
    assert item["source"] == "model"
    assert item["why"] == "继续完善认证模块"
    assert prefetch["anchors"] == ["file:src/auth.py"]  # 幻觉路径完全没有产生任何条目


def test_deep_model_level_prefetch_drops_prediction_when_every_anchor_is_hallucinated(
    store: Store, paths, event_factory, fake_llm
) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(event_factory(status="done", outcome="结论", anchors=Anchors(files=["src/real.py"])))
    store.append(event_factory(status="open", intent="继续处理"))
    fake_llm.queue({"predictions": [{"text": "凭空预测", "anchors": ["src/totally_made_up.py"]}]})

    deep(store, paths, Budget(), fake_llm, NOW)

    assert load_prefetch(paths)["items"] == []


def test_deep_skips_model_level_prefetch_when_no_open_events(store: Store, paths, event_factory, fake_llm) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    store.append(event_factory(status="done", outcome="结论", anchors=Anchors(files=["src/real.py"])))
    # 队列不放 PREFETCH_SYSTEM 的响应：真被调用就会 AssertionError 暴露出来
    deep(store, paths, Budget(), fake_llm, NOW)
    assert load_prefetch(paths)["items"] == []


# ==================================================================== working-set：Likely next 渲染与预算


def _seed_prefetch(paths, items: list[dict[str, Any]]) -> None:
    payload = {
        "generated": NOW.isoformat(timespec="seconds"),
        "items": items,
        "anchors": sorted({f"file:{i['anchor']}" for i in items if i.get("anchor")}),
    }
    atomic_write(prefetch_file(paths), json.dumps(payload, ensure_ascii=False))


def test_working_set_renders_likely_next_section_when_prefetch_has_items(store: Store, paths) -> None:
    _seed_prefetch(
        paths,
        [{"event_id": "2026-08-20_090000", "text": "继续导出模块的分页改造", "anchor": "src/export.py"}],
    )
    rebuild_all(store, paths, Budget(), NOW)

    ws = paths.working_set.read_text(encoding="utf-8")
    assert "## Likely next" in ws
    assert "[2026-08-20_090000] 继续导出模块的分页改造 — 关联: src/export.py" in ws


def test_working_set_omits_likely_next_heading_when_prefetch_json_missing(store: Store, paths) -> None:
    assert not prefetch_file(paths).exists()
    rebuild_all(store, paths, Budget(), NOW)
    assert "## Likely next" not in paths.working_set.read_text(encoding="utf-8")


def test_working_set_omits_likely_next_heading_when_prefetch_items_empty(store: Store, paths) -> None:
    _seed_prefetch(paths, [])
    rebuild_all(store, paths, Budget(), NOW)
    assert "## Likely next" not in paths.working_set.read_text(encoding="utf-8")


def test_working_set_likely_next_is_capped_to_a_third_of_the_working_set_budget(store: Store, paths) -> None:
    _seed_prefetch(
        paths,
        [
            {"event_id": "2026-08-20_090000", "text": "短条目能装下", "anchor": "a.py"},
            {"event_id": "2026-08-20_090100", "text": "X" * 150, "anchor": "b.py"},  # 长到装不下 1/3 预算
        ],
    )
    tiny_budget = Budget(working_set_tokens=60)  # 1/3 份额只有 20 token（约 60 字符）

    rebuild_all(store, paths, tiny_budget, NOW)

    ws = paths.working_set.read_text(encoding="utf-8")
    assert "短条目能装下" in ws
    assert "2026-08-20_090100" not in ws  # 第二条超出 1/3 预算，未被填入


# ==================================================================== prefetch-outcome.jsonl 命中记录


def test_deep_records_prefetch_hit_outcome_by_comparing_old_prediction_to_actual_session(
    store: Store, paths, event_factory
) -> None:
    paths.config.write_text("deep_threshold: 1\n", encoding="utf-8")
    e1 = store.append(event_factory(status="done", outcome="a的结论", anchors=Anchors(files=["src/a.py"])))

    # 模拟上一轮深整理已经写好的预测：两个锚点
    _seed_prefetch(
        paths,
        [{"event_id": e1, "text": "a的结论", "anchor": "src/a.py"}, {"event_id": "x", "text": "y", "anchor": "src/b.py"}],
    )

    session_id = "sess-prefetch-outcome"
    transcript_path = paths.log_dir / f"{session_id}.jsonl"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    _write_transcript(
        transcript_path,
        [
            {
                "type": "assistant",
                "timestamp": "2026-08-25T14:00:00",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "edit1",
                            "name": "Edit",
                            "input": {"file_path": str(paths.project_dir / "src" / "a.py")},
                        }
                    ],
                },
            }
        ],
    )
    surfaced_path = paths.log_dir / f"surfaced-{session_id}.jsonl"
    surfaced_path.write_text(
        json.dumps({"ts": "2026-08-25T13:59:00", "event_id": e1, "cue": "src/a.py", "cue_kind": "file", "chars": 5})
        + "\n",
        encoding="utf-8",
    )

    deep(store, paths, Budget(), None, NOW)

    outcome_path = paths.log_dir / "prefetch-outcome.jsonl"
    assert outcome_path.exists()
    lines = [json.loads(ln) for ln in outcome_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["session"] == session_id
    assert lines[0]["predicted"] == 2
    assert lines[0]["hit"] == 1
    assert surfaced_path.with_name(surfaced_path.name + ".done").exists()
    assert not surfaced_path.exists()
