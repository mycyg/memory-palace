"""PostToolUse hook：按 tool_name 分派的纯查表浮现，不调用 LLM。

可直接执行：python3 -m eventmem.hooks.post_tool_use（stdin 传入官方 hook JSON）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from eventmem.hooks import append_seen, load_seen, run_hook
    from eventmem.index import Budget
    from eventmem.paths import MemoryPaths
    from eventmem.recall import CueKind, SurfaceHit, error_signature, surface
    from eventmem.store import Store
except Exception:  # noqa: BLE001 —— eventmem 包不可用时不能拖垮宿主会话
    try:
        log_path = Path.cwd() / ".memory" / "log" / "eventmem.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("post_tool_use: eventmem 包导入失败，跳过本次浮现\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)

# Read/Edit/Write/MultiEdit/NotebookEdit：均按文件路径查锚点
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
# Bash 失败迹象的粗粒度关键词（与 extract.py 的 _looks_like_error 保持一致的判定口径）
_ERROR_MARKERS = (
    "traceback (most recent call last)",
    "error:",
    "exception:",
    "fatal:",
    "command failed",
)


@dataclass(frozen=True)
class _Surfaced:
    """一次浮现命中及其触发线索，供 §3.13 埋点使用（cue/cue_kind 与 SurfaceHit 一一对应）。"""

    hit: SurfaceHit
    cue: str
    cue_kind: CueKind


def main(payload: dict[str, Any]) -> dict[str, Any] | None:
    """按 tool_name 分派；`.memory` 不存在直接无输出返回（自举留给 session_start）。"""
    project_dir = Path(str(payload.get("cwd") or "."))
    paths = MemoryPaths.for_project(project_dir)
    if not paths.root.is_dir():
        return None

    tool_name = str(payload.get("tool_name") or "")
    session_id = str(payload.get("session_id") or "default")
    tool_input_raw = payload.get("tool_input")
    tool_input = tool_input_raw if isinstance(tool_input_raw, dict) else {}

    store = Store(paths)
    budget = Budget()
    seen = load_seen(paths, session_id)

    surfaced: list[_Surfaced] = []
    if tool_name == "TodoWrite":
        surfaced = _handle_todo_write(tool_input, store, paths, budget, seen)
    elif tool_name in _FILE_TOOLS:
        surfaced = _handle_file_tool(tool_input, store, paths, budget, seen)
    elif tool_name == "Bash":
        surfaced = _handle_bash(payload.get("tool_response"), store, paths, budget, seen)

    if not surfaced:
        return None

    surfaced = surfaced[: budget.surface_k]  # 多条 in_progress todo 各自命中时，总注入量仍不越预算
    hits = [item.hit for item in surfaced]
    append_seen(paths, session_id, [h.event_id for h in hits])
    _log_surfaced(paths, session_id, surfaced)
    lines = "\n".join(h.line for h in hits)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"Memory:\n{lines}",
        }
    }


def _log_surfaced(paths: MemoryPaths, session_id: str, surfaced: list[_Surfaced]) -> None:
    """浮现埋点：每条命中追加一行 log/surfaced-<session_id>.jsonl；写入失败静默（SPEC §3.13）。"""
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        path = paths.log_dir / f"surfaced-{session_id}.jsonl"
        stamp = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            for item in surfaced:
                record = {
                    "ts": stamp,
                    "event_id": item.hit.event_id,
                    "cue": item.cue,
                    "cue_kind": item.cue_kind,
                    "chars": len(item.hit.line),
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 埋点写入失败不影响浮现本身
        pass


def _handle_todo_write(
    tool_input: dict[str, Any],
    store: Store,
    paths: MemoryPaths,
    budget: Budget,
    seen: set[str],
) -> list[_Surfaced]:
    """记录 todo 观察（供人工核查／SessionEnd 抽取旁证），并对 in_progress 的 todo 做意图浮现。

    「同一事件同一会话只浮现一次」由 seen 文件保证，这里不需要额外比对上一次快照
    里谁已经是 in_progress —— 即使同一条 todo 连续多次出现，命中的事件在第一次
    浮现后就进了 seen，后续调用天然拿不到重复结果。每条命中记它自己那条 todo 的
    文本为 cue，多条 todo 各自命中时埋点不会互相沾染。
    """
    todos_raw = tool_input.get("todos")
    todos = [t for t in todos_raw if isinstance(t, dict)] if isinstance(todos_raw, list) else []
    _log_todo_observed(paths, todos)

    surfaced: list[_Surfaced] = []
    seen_local = set(seen)
    for item in todos:
        status = str(item.get("status") or "").strip().lower()
        if status != "in_progress":
            continue
        text = str(item.get("content") or item.get("activeForm") or "").strip()
        if not text:
            continue
        for hit in surface(text, "intent", store, paths, budget, seen_local):
            surfaced.append(_Surfaced(hit=hit, cue=text, cue_kind="intent"))
            seen_local.add(hit.event_id)
    return surfaced


def _log_todo_observed(paths: MemoryPaths, todos: list[dict[str, Any]]) -> None:
    """把本次 todos 原样追加进 log/todo-observed.jsonl；轻量冗余观察，不供程序消费。"""
    if not todos:
        return
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "todos": todos}
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        with (paths.log_dir / "todo-observed.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 冗余观察，失败不影响浮现
        pass


def _handle_file_tool(
    tool_input: dict[str, Any],
    store: Store,
    paths: MemoryPaths,
    budget: Budget,
    seen: set[str],
) -> list[_Surfaced]:
    """Read/Edit/Write/MultiEdit/NotebookEdit：取文件路径查锚点（与 extract.py 同口径的取值顺序）。"""
    file_path = ""
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            file_path = value.strip()
            break
    if not file_path:
        return []
    cue = paths.relative(file_path)
    hits = surface(cue, "file", store, paths, budget, seen)
    return [_Surfaced(hit=h, cue=cue, cue_kind="file") for h in hits]


def _handle_bash(
    tool_response: Any,
    store: Store,
    paths: MemoryPaths,
    budget: Budget,
    seen: set[str],
) -> list[_Surfaced]:
    """Bash：从 tool_response 探测失败迹象，按错误签名查历史 fix 事件的处置路径。"""
    text = _bash_error_text(tool_response)
    if not text:
        return []
    signature = error_signature(text)
    if not signature:
        return []
    hits = surface(signature, "error", store, paths, budget, seen)
    return [_Surfaced(hit=h, cue=signature, cue_kind="error") for h in hits]


def _bash_error_text(tool_response: Any) -> str:
    """从 Bash 的 tool_response 中取失败信号原文；无失败迹象返回空串。

    tool_response 形如 {"stdout", "stderr", "interrupted", "isImage"}：非空 stderr
    是最直接的信号；被中断（超时等）次之；stdout 命中常见报错关键词兜底。
    """
    if not isinstance(tool_response, dict):
        return ""
    stderr = tool_response.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        return stderr
    stdout = tool_response.get("stdout")
    if tool_response.get("interrupted"):
        return stdout if isinstance(stdout, str) and stdout.strip() else "command interrupted"
    if isinstance(stdout, str) and _looks_like_error(stdout):
        return stdout
    return ""


def _looks_like_error(text: str) -> bool:
    head = text[:400].lower()
    return any(marker in head for marker in _ERROR_MARKERS)


if __name__ == "__main__":
    run_hook(main)
