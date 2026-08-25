"""SessionEnd hook：把「抽取（含 LLM 补漏层）→ 轻整理 → 按脏量深整理」整链甩给
一个后台 cli 进程（`extract --then-light`），hook 本体只做 spawn，毫秒级返回。

可直接执行：python3 -m eventmem.hooks.session_end（stdin 传入官方 hook JSON）。

为什么不在 hook 内同步做机械 flush：extract 的水位是机械层与 LLM 层共用的，
同步机械抽取会把水位推到文件尾，后台再跑带 LLM 的抽取时已无行可读——LLM
补漏层（前瞻标记、补充事件）在本路径上会永远空转。串成同一个后台进程后，
水位由唯一的执行者推进，时序与完整性都由它保证；spawn 失败的代价只是本轮
未抽取，水位未动，下一次 PreCompact/SessionEnd 会照常补上（自愈）。
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
            fh.write("session_end: eventmem 包导入失败，跳过本次 flush\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)


def main(payload: dict[str, Any]) -> dict[str, Any] | None:
    """spawn 一个串联的后台进程：抽取 → 轻整理 →（脏量达标时）深整理。"""
    transcript_path = payload.get("transcript_path")
    session_id = str(payload.get("session_id") or "")
    project_dir = str(payload.get("cwd") or ".")

    if transcript_path and session_id:
        spawn_detached(
            [
                sys.executable,
                "-m",
                "eventmem.cli",
                "extract",
                "--transcript",
                str(transcript_path),
                "--session",
                session_id,
                "--then-light",
                "--project",
                project_dir,
            ]
        )
    else:
        # 无 transcript 信息时退回纯整理调度（处理已落盘事件）
        spawn_detached(
            [
                sys.executable,
                "-m",
                "eventmem.cli",
                "consolidate",
                "--light",
                "--deep-if-dirty",
                "--project",
                project_dir,
            ]
        )
    return None


if __name__ == "__main__":
    run_hook(main)
