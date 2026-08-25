"""hooks/（SPEC §3.9）：stdin→stdout 协议护栏、四个 hook 的行为、spawn_detached 的
参数与失败容忍、异常永远静默 exit 0。
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from eventmem import hooks as hooks_pkg
from eventmem.hooks import append_seen, load_seen, run_hook, spawn_detached
from eventmem.hooks import post_tool_use, pre_compact, session_end, session_start
from eventmem.index import Budget, rebuild_all
from eventmem.paths import MemoryPaths
from eventmem.schema import Anchors
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)


# ==================================================================== run_hook：stdin→stdout 协议


def test_run_hook_reads_stdin_and_writes_returned_dict_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    with pytest.raises(SystemExit) as exc_info:
        run_hook(lambda payload: {"foo": "bar"})

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip()) == {"foo": "bar"}


def test_run_hook_writes_nothing_when_main_returns_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    with pytest.raises(SystemExit) as exc_info:
        run_hook(lambda payload: None)

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""


def test_run_hook_exits_zero_and_skips_main_on_invalid_json_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    calls = []

    with pytest.raises(SystemExit) as exc_info:
        run_hook(lambda payload: calls.append(payload))

    assert exc_info.value.code == 0
    assert calls == []
    assert capsys.readouterr().out == ""


def test_run_hook_exits_zero_when_stdin_json_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([1, 2, 3])))
    calls = []

    with pytest.raises(SystemExit) as exc_info:
        run_hook(lambda payload: calls.append(payload))

    assert exc_info.value.code == 0
    assert calls == []


def test_run_hook_exits_zero_and_logs_when_main_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    def boom(payload: dict[str, Any]) -> None:
        raise RuntimeError("模拟的 hook 内部异常")

    with pytest.raises(SystemExit) as exc_info:
        run_hook(boom)

    assert exc_info.value.code == 0
    log_text = MemoryPaths.for_project(tmp_path).log.read_text(encoding="utf-8")
    assert "hook 异常" in log_text
    assert "RuntimeError" in log_text


def test_run_hook_exits_zero_when_return_value_is_not_json_serializable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))

    with pytest.raises(SystemExit) as exc_info:
        run_hook(lambda payload: {"bad": object()})

    assert exc_info.value.code == 0
    log_text = MemoryPaths.for_project(tmp_path).log.read_text(encoding="utf-8")
    assert "hook 输出序列化失败" in log_text


# ==================================================================== spawn_detached


def test_spawn_detached_swallows_popen_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def raising_popen(*args: Any, **kwargs: Any) -> None:
        raise OSError("模拟拉起失败")

    monkeypatch.setattr(hooks_pkg.subprocess, "Popen", raising_popen)

    spawn_detached(["eventmem", "extract", "--project", str(tmp_path)])  # 不应抛异常


class _RecordingPopen:
    """记录构造参数、不真正拉起子进程的 Popen 替身。"""

    calls: list[dict[str, Any]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        _RecordingPopen.calls.append({"argv": argv, **kwargs})


def test_spawn_detached_passes_project_dir_from_double_dash_project_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _RecordingPopen.calls = []
    monkeypatch.setattr(hooks_pkg.subprocess, "Popen", _RecordingPopen)
    project_dir = tmp_path / "myproject"

    spawn_detached(["python3", "-m", "eventmem.cli", "extract", "--project", str(project_dir)])

    assert len(_RecordingPopen.calls) == 1
    call = _RecordingPopen.calls[0]
    assert call["cwd"] == str(project_dir)
    assert call["start_new_session"] is True
    assert (MemoryPaths.for_project(project_dir).log_dir).is_dir()  # log 目录被提前建好


def test_spawn_detached_falls_back_to_cwd_when_no_project_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _RecordingPopen.calls = []
    monkeypatch.setattr(hooks_pkg.subprocess, "Popen", _RecordingPopen)
    monkeypatch.chdir(tmp_path)

    spawn_detached(["python3", "-m", "eventmem.cli", "consolidate", "--light"])

    assert _RecordingPopen.calls[0]["cwd"] == str(tmp_path)


# ==================================================================== load_seen / append_seen


def test_append_seen_then_load_seen_round_trips(paths: MemoryPaths) -> None:
    append_seen(paths, "sess1", ["id1", "id2"])
    assert load_seen(paths, "sess1") == {"id1", "id2"}


def test_load_seen_returns_empty_set_when_file_missing(paths: MemoryPaths) -> None:
    assert load_seen(paths, "never-seen-session") == set()


def test_append_seen_accumulates_across_multiple_calls(paths: MemoryPaths) -> None:
    append_seen(paths, "sess1", ["id1"])
    append_seen(paths, "sess1", ["id2"])
    assert load_seen(paths, "sess1") == {"id1", "id2"}


def test_append_seen_is_scoped_per_session(paths: MemoryPaths) -> None:
    append_seen(paths, "sess1", ["id1"])
    append_seen(paths, "sess2", ["id2"])
    assert load_seen(paths, "sess1") == {"id1"}
    assert load_seen(paths, "sess2") == {"id2"}


# ==================================================================== session_start


def test_session_start_bootstraps_memory_skeleton_when_missing(tmp_path: Path) -> None:
    project_dir = tmp_path / "brandnew"
    assert not (project_dir / ".memory").exists()

    result = session_start.main({"cwd": str(project_dir)})

    assert result is None
    paths = MemoryPaths.for_project(project_dir)
    assert paths.events_dir.is_dir()
    assert paths.index_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_session_start_returns_none_when_working_set_file_is_absent(paths: MemoryPaths) -> None:
    assert not paths.working_set.exists()
    assert session_start.main({"cwd": str(paths.project_dir)}) is None


def test_session_start_returns_none_when_working_set_is_blank(paths: MemoryPaths) -> None:
    paths.working_set.write_text("   \n\n  ", encoding="utf-8")
    assert session_start.main({"cwd": str(paths.project_dir)}) is None


def test_session_start_injects_working_set_and_logs_injected_event(paths: MemoryPaths) -> None:
    content = "# Memory working set (generated x)\n\n## Open events\n- [id] 一条示例\n"
    paths.working_set.write_text(content, encoding="utf-8")

    result = session_start.main({"cwd": str(paths.project_dir), "session_id": "sess-start"})

    assert result == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": content}
    }
    injected_path = paths.log_dir / "injected-sess-start.jsonl"
    records = [json.loads(ln) for ln in injected_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["source"] == "working-set"
    assert records[0]["chars"] == len(content)
    assert "ts" in records[0]


def test_session_start_injects_open_event_from_previous_session(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    """二次 session_start 注入应包含上一会话遗留的 open 事件。"""
    store.append(event_factory(status="open", intent="跨会话延续的任务"))
    rebuild_all(store, paths, Budget(), NOW)

    result = session_start.main({"cwd": str(paths.project_dir), "session_id": "sess2"})

    assert result is not None
    assert "跨会话延续的任务" in result["hookSpecificOutput"]["additionalContext"]


def test_session_start_end_to_end_through_run_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, paths: MemoryPaths
) -> None:
    paths.working_set.write_text("# Memory working set\n\n## Open events\n- [x] 内容\n", encoding="utf-8")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"cwd": str(paths.project_dir), "session_id": "s1"}))
    )

    with pytest.raises(SystemExit) as exc_info:
        run_hook(session_start.main)

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "内容" in payload["hookSpecificOutput"]["additionalContext"]


# ==================================================================== post_tool_use


def test_post_tool_use_returns_none_when_memory_dir_missing(tmp_path: Path) -> None:
    result = post_tool_use.main({"cwd": str(tmp_path / "no-memory-here"), "tool_name": "Bash"})
    assert result is None


def test_post_tool_use_file_tool_surfaces_matching_event(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    e = store.append(
        event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/a.py"]))
    )
    rebuild_all(store, paths, Budget(), NOW)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "a.py")},
            "session_id": "sess1",
        }
    )

    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("Memory:\n")
    assert f"[{e}] 端口冲突已修复" in ctx


def test_post_tool_use_bash_tool_surfaces_matching_error(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    from eventmem.recall import error_signature

    sig = error_signature("ValueError: port busy")
    e = store.append(event_factory(status="done", outcome="改用独立端口区间", anchors=Anchors(error_sigs=[sig])))
    rebuild_all(store, paths, Budget(), NOW)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Bash",
            "tool_response": {"stdout": "", "stderr": "ValueError: port busy"},
            "session_id": "sess1",
        }
    )

    assert result is not None
    assert f"[{e}]" in result["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_todo_write_surfaces_in_progress_todo_by_intent(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    e = store.append(event_factory(status="open", intent="修复端口冲突"))
    rebuild_all(store, paths, Budget(), NOW)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"content": "端口相关的问题", "status": "in_progress"}]},
            "session_id": "sess1",
        }
    )

    assert result is not None
    assert f"[{e}]" in result["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_returns_none_when_nothing_matches(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    store.append(event_factory(status="done", outcome="无关结论", anchors=Anchors(files=["src/a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "unrelated.py")},
            "session_id": "sess1",
        }
    )

    assert result is None


def test_post_tool_use_truncates_combined_hits_across_todos_to_surface_k(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    """多条 in_progress todo 各自命中时，总注入量仍不超过 surface_k（默认 3）。"""
    for outcome in ("结论1", "结论2", "结论3"):
        store.append(event_factory(status="done", outcome=outcome, intent="重构模块Alpha方案"))
    for outcome in ("结论4", "结论5"):
        store.append(event_factory(status="done", outcome=outcome, intent="重构模块Beta方案"))
    rebuild_all(store, paths, Budget(), NOW)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {"content": "重构模块Alpha方案", "status": "in_progress"},
                    {"content": "重构模块Beta方案", "status": "in_progress"},
                ]
            },
            "session_id": "sess-multi-todo",
        }
    )

    assert result is not None
    lines = result["hookSpecificOutput"]["additionalContext"].split("\n")[1:]  # 去掉 "Memory:" 标题行
    assert len(lines) == 3


def test_post_tool_use_writes_seen_after_surfacing(store: Store, paths: MemoryPaths, event_factory) -> None:
    e = store.append(event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "a.py")},
            "session_id": "sess-seen",
        }
    )

    assert e in load_seen(paths, "sess-seen")


def test_post_tool_use_does_not_resurface_an_event_already_seen_this_session(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    e = store.append(event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/a.py"])))
    rebuild_all(store, paths, Budget(), NOW)
    append_seen(paths, "sess-preseen", [e])

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "a.py")},
            "session_id": "sess-preseen",
        }
    )

    assert result is None


def test_post_tool_use_surfaced_jsonl_has_exactly_five_keys(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    store.append(event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/a.py"])))
    rebuild_all(store, paths, Budget(), NOW)

    post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "a.py")},
            "session_id": "sess-fmt",
        }
    )

    surfaced_path = paths.log_dir / "surfaced-sess-fmt.jsonl"
    records = [json.loads(ln) for ln in surfaced_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert set(records[0].keys()) == {"ts", "event_id", "cue", "cue_kind", "chars"}
    assert records[0]["cue_kind"] == "file"
    assert records[0]["cue"] == "src/a.py"
    assert records[0]["chars"] == len(records[0]["event_id"]) or records[0]["chars"] > 0


def test_post_tool_use_surfaced_log_write_failure_does_not_break_main_output(
    store: Store, paths: MemoryPaths, event_factory
) -> None:
    """埋点写失败不影响主输出：把目标 jsonl 路径预先做成一个目录，_log_surfaced 内部
    open() 必然失败，但应被静默吞掉，浮现结果照常返回。"""
    e = store.append(event_factory(status="done", outcome="端口冲突已修复", anchors=Anchors(files=["src/a.py"])))
    rebuild_all(store, paths, Budget(), NOW)
    session_id = "sess-logfail"
    (paths.log_dir / f"surfaced-{session_id}.jsonl").mkdir(parents=True)

    result = post_tool_use.main(
        {
            "cwd": str(paths.project_dir),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(paths.project_dir / "src" / "a.py")},
            "session_id": session_id,
        }
    )

    assert result is not None
    assert f"[{e}]" in result["hookSpecificOutput"]["additionalContext"]


def test_post_tool_use_end_to_end_exits_cleanly_when_store_construction_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, paths: MemoryPaths
) -> None:
    """即使内部构造 Store 时炸了，走完整的 run_hook 包装仍应静默 exit 0。"""

    def raising_store(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("模拟 Store 构造失败")

    monkeypatch.setattr(post_tool_use, "Store", raising_store)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "cwd": str(paths.project_dir),
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/a.py"},
                    "session_id": "sess-crash",
                }
            )
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        run_hook(post_tool_use.main)

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""
    assert "hook 异常" in paths.log.read_text(encoding="utf-8")


# ==================================================================== pre_compact


def test_pre_compact_spawns_extract_with_expected_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pre_compact, "spawn_detached", lambda argv: calls.append(argv))
    project_dir = str(tmp_path / "proj")

    result = pre_compact.main(
        {"transcript_path": "/tmp/some.jsonl", "session_id": "sess1", "cwd": project_dir}
    )

    assert result is None
    assert calls == [
        [
            sys.executable,
            "-m",
            "eventmem.cli",
            "extract",
            "--transcript",
            "/tmp/some.jsonl",
            "--session",
            "sess1",
            "--project",
            project_dir,
        ]
    ]


def test_pre_compact_skips_spawn_when_transcript_path_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pre_compact, "spawn_detached", lambda argv: calls.append(argv))

    assert pre_compact.main({"session_id": "sess1", "cwd": "."}) is None
    assert calls == []


def test_pre_compact_skips_spawn_when_session_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(pre_compact, "spawn_detached", lambda argv: calls.append(argv))

    assert pre_compact.main({"transcript_path": "/tmp/x.jsonl", "cwd": "."}) is None
    assert calls == []


# ==================================================================== session_end


def test_session_end_flushes_mechanically_then_spawns_consolidate(
    monkeypatch: pytest.MonkeyPatch, paths: MemoryPaths
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(session_end, "spawn_detached", lambda argv: calls.append(argv))
    transcript = paths.project_dir / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "session_end 机械抽取测试", "status": "in_progress"}]},
                    }
                ],
            },
        }
    ]
    transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    result = session_end.main(
        {"transcript_path": str(transcript), "session_id": "sess-end", "cwd": str(paths.project_dir)}
    )

    assert result is None
    # 集成裁决：hook 不再同步做机械 flush（那会抢先推水位，让后台 LLM 补漏层空转），
    # 整链（extract --then-light）交给唯一的后台进程；hook 本体不写任何事件
    store = Store(paths)
    assert list(store.iter_events()) == []
    assert calls == [
        [
            sys.executable,
            "-m",
            "eventmem.cli",
            "extract",
            "--transcript",
            str(transcript),
            "--session",
            "sess-end",
            "--then-light",
            "--project",
            str(paths.project_dir),
        ]
    ]


def test_session_end_spawns_consolidate_even_when_transcript_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(session_end, "spawn_detached", lambda argv: calls.append(argv))

    result = session_end.main({"cwd": str(tmp_path / "proj")})

    assert result is None
    assert len(calls) == 1
    assert calls[0][:4] == [sys.executable, "-m", "eventmem.cli", "consolidate"]


def test_session_end_spawns_chained_extract_for_any_transcript_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有 transcript 信息就 spawn 串联抽取通道（--then-light）；hook 本体不校验
    路径是否存在——存在性问题由后台进程自行处理并记日志。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(session_end, "spawn_detached", lambda argv: calls.append(argv))
    project_dir = tmp_path / "proj"

    result = session_end.main(
        {"transcript_path": "/tmp/whatever.jsonl", "session_id": "sess1", "cwd": str(project_dir)}
    )

    assert result is None
    assert len(calls) == 1
    assert calls[0][3] == "extract"
    assert "--then-light" in calls[0]


def test_session_end_end_to_end_through_run_hook_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_end, "spawn_detached", lambda argv: None)
    project_dir = tmp_path / "proj"
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"cwd": str(project_dir), "session_id": "s1"}))
    )

    with pytest.raises(SystemExit) as exc_info:
        run_hook(session_end.main)

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == ""  # session_end 永不产生 stdout 输出
