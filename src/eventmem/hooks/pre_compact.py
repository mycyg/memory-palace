"""PreCompact hook：compact 前的抢救式 flush，抽取工作甩给后台 cli 进程。

可直接执行：python3 -m eventmem.hooks.pre_compact（stdin 传入官方 hook JSON）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from eventmem.hooks import run_hook, spawn_detached
except Exception:  # noqa: BLE001 —— eventmem 包不可用时不能拖垮宿主会话
    try:
        log_path = Path.cwd() / ".memory" / "log" / "eventmem.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("pre_compact: eventmem 包导入失败，跳过本次后台抽取\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)


def main(payload: dict[str, Any]) -> dict[str, Any] | None:
    """把 `eventmem extract` 甩到后台；不阻塞 compact、不输出、不做任何判断。"""
    transcript_path = payload.get("transcript_path")
    session_id = payload.get("session_id")
    if not transcript_path or not session_id:
        return None  # 字段缺失：没有可抽取的目标，静默跳过

    project_dir = str(payload.get("cwd") or ".")
    spawn_detached(
        [
            sys.executable,
            "-m",
            "eventmem.cli",
            "extract",
            "--transcript",
            str(transcript_path),
            "--session",
            str(session_id),
            "--project",
            project_dir,
        ]
    )
    return None


if __name__ == "__main__":
    run_hook(main)
