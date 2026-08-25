"""hooks 公共层：stdin/stdout 协议护栏、后台拉起、seen 集合读写。

硬纪律（SPEC §3.9）：hook 进程永不非零退出、永不把异常抛到顶层、同步路径永不
超过 5 秒、hook 进程内绝不调用 LLM（LLM 类工作一律 spawn_detached 到后台）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from ..paths import MemoryPaths

__all__ = ["run_hook", "spawn_detached", "load_seen", "append_seen"]


def _load_env(project_dir: Path) -> None:
    """加载 EVENTMEM_* 环境变量：先读 <project>/.env，再读 ~/.claude/eventmem.env。

    手写解析，不引 dotenv：跳过空行与 # 注释行，KEY=VALUE 按首个 = 切分，值两侧
    的匹配引号剥掉。已存在的环境变量、以及先读到的文件里已设的值，都不被覆盖。
    """
    for path in (project_dir / ".env", Path.home() / ".claude" / "eventmem.env"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _guard_log(project_dir: Path, message: str) -> None:
    """护栏日志：尽力而为，写日志本身绝不抛异常（否则失去了护栏的意义）。"""
    try:
        paths = MemoryPaths.for_project(project_dir)
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with paths.log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001 —— 日志失败静默
        pass


def run_hook(main: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
    """hook 入口护栏：stdin 读 JSON → main(payload) → 有返回值则 stdout 打 JSON。

    任何异常、任何非 JSON / 非对象的 stdin 输入，都静默 exit 0：绝不非零退出，
    绝不把异常抛到调用方（Claude Code 本体）。project 根取 payload 的 cwd 字段，
    缺失时回退 os.getcwd()。
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook 输入不是 JSON 对象")
    except Exception:  # noqa: BLE001 —— stdin 非 JSON：静默退出，不调用 main
        sys.exit(0)

    project_dir = Path(str(payload.get("cwd") or os.getcwd()))
    try:
        _load_env(project_dir)
    except Exception:  # noqa: BLE001 —— env 加载失败不影响主流程
        pass

    try:
        result = main(payload)
    except Exception as exc:  # noqa: BLE001 —— 硬纪律：永不把异常抛到顶层
        _guard_log(project_dir, f"hook 异常 {type(exc).__name__}: {exc}")
        sys.exit(0)

    if isinstance(result, dict):
        try:
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 —— 序列化失败也不能让 hook 非零退出
            _guard_log(project_dir, f"hook 输出序列化失败 {exc}")
    sys.exit(0)


def _extract_project_arg(argv: list[str]) -> Path | None:
    """从 argv 里取 `--project <dir>` 的值；不存在或形式不对返回 None。"""
    for i, token in enumerate(argv):
        if token == "--project" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if token.startswith("--project="):
            return Path(token.split("=", 1)[1])
    return None


def spawn_detached(argv: list[str]) -> None:
    """把 LLM 类工作 fork 到后台：脱离当前会话，输出重定向到护栏日志。

    永不阻塞、永不向调用方抛异常；拉起失败只记日志（若可能）。日志落在哪个项目的
    `.memory/log/` 下，优先取 argv 里显式的 `--project <dir>`（本包两个调用方
    都会带上）；没有才退回当前目录 —— hook 进程按 Claude Code 的约定本就运行在
    项目根，但显式参数更可靠，不依赖这条约定总是成立（例如手工调试时终端 cwd
    未必等于 payload 里的 cwd）。
    """
    project_dir = _extract_project_arg(argv) or Path.cwd()
    try:
        paths = MemoryPaths.for_project(project_dir)
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        log_fh = paths.log.open("a", encoding="utf-8")
        try:
            subprocess.Popen(
                argv,
                stdout=log_fh,
                stderr=log_fh,
                stdin=subprocess.DEVNULL,
                cwd=str(project_dir),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_fh.close()  # 父进程侧的文件对象可以立即关闭，子进程已继承了描述符
    except Exception:  # noqa: BLE001 —— 后台拉起失败不影响 hook 本身
        pass


def load_seen(paths: MemoryPaths, session_id: str) -> set[str]:
    """读取本会话已浮现过的事件 id 集合；文件不存在或不可读返回空集合。"""
    try:
        text = paths.seen_file(session_id).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def append_seen(paths: MemoryPaths, session_id: str, event_ids: Iterable[str]) -> None:
    """把新命中的事件 id 追加进本会话的 seen 文件，每行一个；写入失败静默。"""
    ids = [i for i in event_ids if i]
    if not ids:
        return
    try:
        path = paths.seen_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for event_id in ids:
                fh.write(event_id + "\n")
    except Exception:  # noqa: BLE001 —— seen 写入失败不影响本次浮现结果
        pass
