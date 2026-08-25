"""子 agent 的事件归属（SPEC §3.17）：extract 机械层把 Task/Agent/Subagent 工具调用
识别为一个委托事件，返回文本先过 scrub 再抽锚点，文件锚点须项目内实际存在。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from datetime import datetime

from eventmem.extract import extract_events
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    }


def _tool_result(tool_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        },
    }


def _real_file(tmp_path: Path, rel: str) -> None:
    path = tmp_path / "project" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 占位内容\n", encoding="utf-8")


# ==================================================================== 基本字段


def test_delegation_creates_build_event_with_expected_fields(store: Store, paths, tmp_path: Path) -> None:
    _real_file(tmp_path, "src/auth.py")
    records = [
        _tool_use("task1", "Task", {"description": "调研认证方案的历史决策", "subagent_type": "general-purpose"}),
        _tool_result("task1", "已完成调研，采用 JWT 方案。参考文件 src/auth.py。commit a3f21c9 已提交。"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-deleg", NOW)

    assert len(created) == 1
    event = store.read(created[0])
    assert event.kind == "build"
    assert event.body == "委托: general-purpose"
    assert event.status == "done"
    assert event.intent == "调研认证方案的历史决策"
    assert event.outcome == "已完成调研，采用 JWT 方案。参考文件 src/auth.py。commit a3f21c9 已提交。"
    assert event.anchors.files == ["src/auth.py"]
    assert event.anchors.commits == ["a3f21c9"]
    assert event.anchors.dialog == ["sess-deleg#L1-L2"]


def test_delegation_without_result_stays_open_with_no_outcome(store: Store, paths, tmp_path: Path) -> None:
    records = [_tool_use("task1", "Task", {"description": "一个尚未返回结果的委托任务"})]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-open", NOW)

    assert len(created) == 1
    event = store.read(created[0])
    assert event.status == "open"
    assert event.outcome is None


def test_delegation_intent_truncated_to_eighty_chars(store: Store, paths, tmp_path: Path) -> None:
    long_description = "A" * 100
    records = [
        _tool_use("task1", "Task", {"description": long_description}),
        _tool_result("task1", "已完成"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-trunc-intent", NOW)

    intent = store.read(created[0]).intent
    assert len(intent) == 80
    assert intent == "A" * 80


def test_delegation_outcome_truncated_to_two_hundred_chars(store: Store, paths, tmp_path: Path) -> None:
    long_result = "B" * 250
    records = [
        _tool_use("task1", "Task", {"description": "一个返回超长结果的委托任务"}),
        _tool_result("task1", long_result),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-trunc-outcome", NOW)

    outcome = store.read(created[0]).outcome
    assert len(outcome) == 200
    assert outcome == "B" * 200


# ==================================================================== scrub 先行于锚点抽取


def test_delegation_result_is_scrubbed_before_outcome_and_anchor_extraction(
    store: Store, paths, tmp_path: Path
) -> None:
    """返回文本先过 scrub 再抽锚点：密钥里恰好形似 hex 的片段不应产生 commit 锚点，
    也不应原样出现在落盘的 outcome 里。"""
    _real_file(tmp_path, "src/real.py")
    records = [
        _tool_use("task1", "Task", {"description": "排查鉴权配置问题"}),
        _tool_result("task1", "token: abcdef0123456789ab 已记录，commit a3f21c9 已推送，参考 src/real.py"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-scrub", NOW)

    event = store.read(created[0])
    assert "abcdef0123456789ab" not in event.outcome
    assert "<REDACTED:secret>" in event.outcome
    assert event.anchors.commits == ["a3f21c9"]  # 密钥里的 hex 片段没有混进锚点
    assert event.anchors.files == ["src/real.py"]


# ==================================================================== 文件锚点须项目内实际存在


def test_delegation_file_anchor_requires_actual_existence_on_disk(store: Store, paths, tmp_path: Path) -> None:
    _real_file(tmp_path, "src/real.py")
    records = [
        _tool_use("task1", "Task", {"description": "整理一份模块清单"}),
        _tool_result("task1", "涉及文件 src/real.py 与 src/ghost_module.py，后者其实并不存在"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-ghost", NOW)

    assert store.read(created[0]).anchors.files == ["src/real.py"]


# ==================================================================== 工具名与 body 前缀


def test_delegation_body_falls_back_to_tool_name_when_subagent_type_missing(
    store: Store, paths, tmp_path: Path
) -> None:
    records = [
        _tool_use("agent1", "Agent", {"description": "没有 subagent_type 字段的委托"}),
        _tool_result("agent1", "已完成"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-agent", NOW)

    assert store.read(created[0]).body == "委托: Agent"


def test_dsh_subagent_tool_alias_is_recognized(store: Store, paths, tmp_path: Path) -> None:
    """dsh 侧 feed 把工具名写成 Subagent（首字母大写化后的通用别名）。"""
    records = [
        _tool_use("sa1", "Subagent", {"description": "dsh 侧委托任务描述", "subagent_type": "reviewer"}),
        _tool_result("sa1", "已完成审阅"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-subagent", NOW)

    assert len(created) == 1
    assert store.read(created[0]).body == "委托: reviewer"


def test_delegation_uses_prompt_field_when_description_absent(store: Store, paths, tmp_path: Path) -> None:
    records = [
        _tool_use("task1", "Task", {"prompt": "使用 prompt 字段而非 description"}),
        _tool_result("task1", "已完成"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-prompt-field", NOW)

    assert store.read(created[0]).intent == "使用 prompt 字段而非 description"


# ==================================================================== 边界与多委托


def test_delegation_with_too_short_description_is_skipped(store: Store, paths, tmp_path: Path) -> None:
    records = [_tool_use("task1", "Task", {"description": "abc"})]  # 少于 4 字符
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-tooshort", NOW)

    assert created == []


def test_multiple_delegations_in_one_window_produce_separate_events(store: Store, paths, tmp_path: Path) -> None:
    records = [
        _tool_use("task1", "Task", {"description": "第一个委托任务", "subagent_type": "explorer"}),
        _tool_result("task1", "第一个任务完成"),
        _tool_use("task2", "Task", {"description": "第二个委托任务", "subagent_type": "reviewer"}),
        _tool_result("task2", "第二个任务完成"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-multi", NOW)

    assert len(created) == 2
    intents = {store.read(eid).intent for eid in created}
    assert intents == {"第一个委托任务", "第二个委托任务"}
    bodies = {store.read(eid).body for eid in created}
    assert bodies == {"委托: explorer", "委托: reviewer"}
