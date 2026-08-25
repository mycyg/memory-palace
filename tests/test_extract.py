"""extract.py：transcript（jsonl）→ 事件。

机械收集（TodoWrite/Bash git commit/报错/Read/Edit 文件路径）不调 LLM；
LLM 判断层只在给出 client 时运行，产出经白名单过滤后落盘。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eventmem.extract import extract_events, load_todo_state
from eventmem.llm import LLMError
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)


# ---------------------------------------------------------------- transcript 构造小工具


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _user_text(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    }


def _tool_result(tool_id: str, content: str, *, is_error: bool = False, tool_use_result: dict | None = None) -> dict:
    record: dict[str, Any] = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
            ],
        },
    }
    if tool_use_result is not None:
        record["toolUseResult"] = tool_use_result
    return record


def _todo_write(tool_id: str, content: str, status: str) -> dict[str, Any]:
    return _assistant_tool_use(tool_id, "TodoWrite", {"todos": [{"content": content, "status": status}]})


# ---------------------------------------------------------------- 机械收集


def test_mechanical_harvest_opens_event_and_merges_anchors(store: Store, paths, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    file_path = project_dir / "train" / "launcher.py"

    records = [
        _user_text("帮我修一下端口冲突的问题"),
        _todo_write("todo1", "修复 Ray 端口冲突", "in_progress"),
        _assistant_tool_use("edit1", "Edit", {"file_path": str(file_path), "old_string": "a", "new_string": "b"}),
        _tool_result("edit1", "文件已更新"),
        _assistant_tool_use("bash1", "Bash", {"command": "pytest tests/test_launcher.py"}),
        _tool_result(
            "bash1",
            "Traceback (most recent call last):\nValueError: port busy",
            is_error=True,
            tool_use_result={
                "stdout": "",
                "stderr": (
                    "Traceback (most recent call last):\n"
                    '  File "launcher.py", line 10, in <module>\n'
                    "    raise ValueError('port busy')\n"
                    "ValueError: port busy"
                ),
            },
        ),
        _assistant_tool_use("bash2", "Bash", {"command": "git commit -am 'fix: port conflict'"}),
        _tool_result(
            "bash2",
            "[main a3f21c9] fix: port conflict",
            tool_use_result={"stdout": "[main a3f21c9] fix: port conflict\n 1 file changed", "stderr": ""},
        ),
        _todo_write("todo2", "修复 Ray 端口冲突", "completed"),
        _assistant_text("搞定，端口冲突已修复。"),
        "{not valid json,,,",  # 无关／坏行：应被跳过而不是让整次抽取失败
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-abc", NOW)

    assert len(created) == 1
    event = store.read(created[0])
    # 机械层只负责开事件与挂锚点，不闭合（闭合是 consolidate.light 的规则闭合职责）
    assert event.status == "open"
    assert event.kind == "build"
    assert event.intent == "修复 Ray 端口冲突"
    assert event.anchors.commits == ["a3f21c9"]
    assert event.anchors.files == ["train/launcher.py"]
    assert event.anchors.tests == ["pytest tests/test_launcher.py"]
    assert event.anchors.error_sigs == ["ValueError: port busy"]
    assert event.anchors.dialog == ["sess-abc#L2-L9"]


def test_mechanical_harvest_records_completed_todo_in_todo_state(store: Store, paths, tmp_path: Path) -> None:
    records = [
        _todo_write("t1", "构建导出功能", "in_progress"),
        _todo_write("t2", "构建导出功能", "completed"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-todo", NOW)
    event_id = created[0]

    state = load_todo_state(paths)
    key = "构建导出功能".lower()
    assert state[key]["status"] == "completed"
    assert state[key]["event_id"] == event_id
    # 事件本身仍是 open：闭合是 consolidate.light 的职责，不是抽取层的职责
    assert store.read(event_id).status == "open"


def test_irrelevant_lines_produce_no_events_or_anchors(store: Store, paths, tmp_path: Path) -> None:
    records = [
        _user_text("随便聊聊今天天气"),
        _assistant_text("好的，今天天气不错。"),
        _assistant_tool_use("bash1", "Bash", {"command": "ls -la"}),
        _tool_result("bash1", "total 0\ndrwxr-xr-x  2 a  staff  64 Jan  1 00:00 .", tool_use_result={"stdout": "total 0", "stderr": ""}),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-noise", NOW)
    assert created == []
    assert store.all_ids() == []


# ---------------------------------------------------------------- 水位


def test_watermark_advances_to_total_line_count(store: Store, paths, tmp_path: Path) -> None:
    records = [_user_text(f"turn {i}") for i in range(4)]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    extract_events(transcript_path, store, None, "sess-wm", NOW)
    assert paths.extract_watermark("sess-wm").read_text(encoding="utf-8").strip() == "4"


def test_second_call_with_unchanged_transcript_finds_no_new_lines(store: Store, paths, tmp_path: Path) -> None:
    records = [_todo_write("t1", "任务A", "in_progress")]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    first = extract_events(transcript_path, store, None, "sess-repeat", NOW)
    second = extract_events(transcript_path, store, None, "sess-repeat", NOW)
    assert len(first) == 1
    assert second == []  # 水位已推进到文件末尾，没有新行可处理


def test_watermark_resets_and_rescans_when_transcript_shrinks(store: Store, paths, tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, [_user_text(f"turn {i}") for i in range(5)])

    extract_events(transcript_path, store, None, "sess-shrink", NOW)
    assert paths.extract_watermark("sess-shrink").read_text(encoding="utf-8").strip() == "5"

    # 模拟 transcript 被截断／替换成一个更短的文件（真实场景：新会话复用了同一 session_id）
    _write_transcript(transcript_path, [_todo_write("t1", "新会话重置后的任务", "in_progress")])
    created = extract_events(transcript_path, store, None, "sess-shrink", NOW)

    assert len(created) == 1
    assert store.read(created[0]).intent == "新会话重置后的任务"
    assert paths.extract_watermark("sess-shrink").read_text(encoding="utf-8").strip() == "1"


# ---------------------------------------------------------------- client=None 纯机械模式


def test_client_none_extracts_mechanical_events_without_llm(store: Store, paths, tmp_path: Path) -> None:
    records = [_todo_write("t1", "纯机械模式任务", "in_progress")]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-mech", NOW)
    assert len(created) == 1
    assert store.read(created[0]).intent == "纯机械模式任务"


# ---------------------------------------------------------------- LLM 补充事件：白名单过滤


def _harvest_transcript_with_one_real_file(tmp_path: Path, real_file: Path) -> Path:
    records = [
        _user_text("看看这个模块要怎么设计"),
        _assistant_tool_use("read1", "Read", {"file_path": str(real_file)}),
        _tool_result("read1", "file contents..."),
        _assistant_text("决定用方案A而不是方案B，因为B需要额外依赖"),
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)
    return transcript_path


def test_llm_phase_filters_hallucinated_anchors_not_in_mechanical_harvest(
    store: Store, paths, tmp_path: Path, fake_llm
) -> None:
    real_file = tmp_path / "project" / "src" / "real.py"
    transcript_path = _harvest_transcript_with_one_real_file(tmp_path, real_file)

    fake_llm.queue(
        {
            "events": [
                {
                    "kind": "decision",
                    "status": "done",
                    "intent": "选择方案A而非方案B",
                    "outcome": "采用方案A",
                    "anchors": {
                        "files": ["src/real.py", "src/hallucinated_ghost.py"],
                        "commits": ["deadbeef0"],
                        "error_sigs": [],
                        "dialog": [],
                    },
                }
            ]
        }
    )

    created = extract_events(transcript_path, store, fake_llm, "sess-halluc", NOW)
    assert len(created) == 1
    event = store.read(created[0])
    assert event.anchors.files == ["src/real.py"]  # 幻觉路径被剔除，真实路径保留
    assert event.anchors.commits == []  # 幻觉 commit 未出现在机械收集里，整体丢弃


def test_llm_phase_maps_kind_alias_to_canonical_value(store: Store, paths, tmp_path: Path, fake_llm) -> None:
    real_file = tmp_path / "project" / "src" / "real.py"
    transcript_path = _harvest_transcript_with_one_real_file(tmp_path, real_file)
    fake_llm.queue(
        {"events": [{"kind": "refactor", "status": "done", "intent": "重构辅助函数为独立模块", "outcome": "拆分完成"}]}
    )

    created = extract_events(transcript_path, store, fake_llm, "sess-alias", NOW)
    assert len(created) == 1
    assert store.read(created[0]).kind == "build"  # refactor 是 build 的别名


def test_llm_phase_drops_event_with_unmappable_kind(store: Store, paths, tmp_path: Path, fake_llm) -> None:
    real_file = tmp_path / "project" / "src" / "real.py"
    transcript_path = _harvest_transcript_with_one_real_file(tmp_path, real_file)
    fake_llm.queue(
        {
            "events": [
                {"kind": "not_a_real_kind", "status": "done", "intent": "这条应该被丢弃", "outcome": "x"},
                {"kind": "fix", "status": "done", "intent": "这条应该保留", "outcome": "y"},
            ]
        }
    )

    created = extract_events(transcript_path, store, fake_llm, "sess-badkind", NOW)
    assert len(created) == 1
    assert store.read(created[0]).intent == "这条应该保留"


def test_llm_phase_coerces_illegal_status_away_from_superseded(
    store: Store, paths, tmp_path: Path, fake_llm
) -> None:
    """抽取层没有 supersede 机制；LLM 声称的 superseded 状态必须被拦下，不能直接落盘。"""
    real_file = tmp_path / "project" / "src" / "real.py"
    transcript_path = _harvest_transcript_with_one_real_file(tmp_path, real_file)
    fake_llm.queue(
        {"events": [{"kind": "fix", "status": "superseded", "intent": "声称是superseded状态", "outcome": "y"}]}
    )

    created = extract_events(transcript_path, store, fake_llm, "sess-supersede", NOW)
    assert len(created) == 1
    event = store.read(created[0])
    assert event.status != "superseded"
    assert event.status == "done"  # 给了 outcome 时回落为 done，而不是原样接受非法状态


# ---------------------------------------------------------------- LLM 失败时机械结果仍落盘


def test_llm_error_does_not_lose_mechanical_results(store: Store, paths, tmp_path: Path, fake_llm) -> None:
    records = [_todo_write("t1", "构建缓存层", "in_progress")]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)
    fake_llm.queue(LLMError("模拟网络失败"))

    created = extract_events(transcript_path, store, fake_llm, "sess-err", NOW)

    assert len(created) == 1  # 机械收集的事件不受 LLM 失败影响
    assert store.read(created[0]).intent == "构建缓存层"
    # 水位仍然推进，不会因为 LLM 失败而反复重扫同一批行
    assert paths.extract_watermark("sess-err").read_text(encoding="utf-8").strip() == "1"


def test_llm_error_does_not_propagate_out_of_extract_events(store: Store, paths, tmp_path: Path, fake_llm) -> None:
    real_file = tmp_path / "project" / "src" / "real.py"
    transcript_path = _harvest_transcript_with_one_real_file(tmp_path, real_file)
    fake_llm.queue(LLMError("boom"))

    created = extract_events(transcript_path, store, fake_llm, "sess-err2", NOW)  # 不应抛出
    assert created == []  # 没有机械事件，也没有 LLM 补充事件
