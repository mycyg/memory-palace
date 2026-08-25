"""SessionStart hook：注入工作集；`.memory/` 不存在时零配置自举。

可直接执行：python3 -m eventmem.hooks.session_start（stdin 传入官方 hook JSON）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from eventmem.hooks import run_hook
    from eventmem.paths import MemoryPaths
except Exception:  # noqa: BLE001 —— eventmem 包不可用时不能拖垮宿主会话
    try:
        log_path = Path.cwd() / ".memory" / "log" / "eventmem.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("session_start: eventmem 包导入失败，跳过本次注入\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)


def main(payload: dict[str, Any]) -> dict[str, Any] | None:
    """读 working-set.md 全文注入；`.memory/` 不存在则只建骨架，本次不注入。"""
    project_dir = Path(str(payload.get("cwd") or "."))
    paths = MemoryPaths.for_project(project_dir)

    if not paths.root.is_dir():
        paths.ensure()  # 零配置自举：骨架建好，尚无工作集内容可注入
        return None

    try:
        text = paths.working_set.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 缺失/不可读都视为无工作集
        return None
    if not text.strip():
        return None

    session_id = str(payload.get("session_id") or "default")
    _log_injected(paths, session_id, text)

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }


def _log_injected(paths: MemoryPaths, session_id: str, text: str) -> None:
    """注入埋点：追加 log/injected-<session_id>.jsonl；写入失败静默（SPEC §3.13）。"""
    try:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        path = paths.log_dir / f"injected-{session_id}.jsonl"
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": "working-set",
            "chars": len(text),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 埋点写入失败不影响本次注入
        pass


if __name__ == "__main__":
    run_hook(main)
