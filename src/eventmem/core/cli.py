from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from .engine import Engine
from .models import RecallRequest, RevisionInput, SourceInput

COMMANDS = {
    "serve",
    "console",
    "mcp",
    "receive",
    "recall",
    "memory",
    "correct",
    "worker",
    "migrate",
    "backup",
    "restore",
    "export",
    "api",
    "contact",
    "evaluate",
    "benchmark",
    "host",
}


def parser():
    root = argparse.ArgumentParser(
        prog="eventmem", description="MemoryPalace 1.0 unified service and tools"
    )
    sub = root.add_subparsers(dest="command", required=True)
    for command in sorted(COMMANDS):
        p = sub.add_parser(command)
        p.add_argument(
            "--root",
            type=Path,
            default=Path(
                os.environ.get("EVENTMEM_HOME", str(Path.home() / ".memorypalace"))
            ),
        )
        if command in {"serve", "console"}:
            p.add_argument("--port", type=int, default=8319)
            p.add_argument("--no-worker", action="store_true")
        elif command in {"receive", "recall", "correct", "contact", "host"}:
            p.add_argument("--json", help="JSON payload; omit to read stdin")
            if command == "correct":
                p.add_argument("id")
            if command == "host":
                p.add_argument(
                    "event",
                    choices=[
                        "start",
                        "pre_action",
                        "tool",
                        "message",
                        "compact",
                        "end",
                        "boundary",
                    ],
                )
        elif command == "memory":
            p.add_argument("id")
            p.add_argument("--known-at")
            p.add_argument("--at")
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--length", type=int, default=12000)
            p.add_argument("--budget", type=int, default=4000)
            p.add_argument("--session")
        elif command == "worker":
            p.add_argument("--once", action="store_true")
        elif command == "mcp":
            p.add_argument(
                "--transport", choices=["stdio", "streamable-http"], default="stdio"
            )
            p.add_argument("--port", type=int, default=8319)
        elif command == "migrate":
            p.add_argument("legacy", type=Path)
            p.add_argument("--scope", default="{}")
        elif command in {"backup", "restore", "export"}:
            p.add_argument("path", type=Path)
        elif command == "api":
            p.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
            p.add_argument("path")
            p.add_argument("--json")
            p.add_argument("--url", default="http://127.0.0.1:8319")
        elif command in {"evaluate", "benchmark"}:
            p.add_argument("--output", type=Path, required=True)
            p.add_argument("--scale", choices=["smoke", "full"], default="smoke")
            p.add_argument("--dataset", type=Path)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command in {"migrate", "restore"}:
        from .transfer import migrate, restore
        from .models import Scope

        result = (
            migrate(args.legacy, args.root, Scope.model_validate_json(args.scope))
            if args.command == "migrate"
            else restore(args.path, args.root)
        )
    elif args.command in {"evaluate", "benchmark"}:
        from .evaluation import evaluate, benchmark

        result = (
            evaluate(args.output, args.dataset)
            if args.command == "evaluate"
            else benchmark(args.root, args.output, args.scale)
        )
    else:
        engine = Engine(args.root)
        command = args.command
        if (
            command in {"serve", "console"}
            or command == "mcp"
            and args.transport == "streamable-http"
        ):
            from .api import create_app, credential
            import uvicorn

            app = create_app(
                engine=engine, workers=not getattr(args, "no_worker", False)
            )
            if command == "console":
                webbrowser.open(
                    f"http://127.0.0.1:{args.port}/#token={credential(engine.db.root)}"
                )
            print(
                f"MemoryPalace http://127.0.0.1:{args.port} · credential: {engine.db.root / 'local-token'}",
                file=sys.stderr,
            )
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
            return 0
        if command == "mcp":
            from .mcp import create_mcp

            create_mcp(engine).run(transport="stdio")
            return 0
        if command == "worker":
            from .jobs import Worker

            worker = Worker(engine)
            if args.once:
                result = {"ran": worker.run_once()}
            else:
                worker.run()
                return 0
        elif command in {"receive", "recall", "correct", "contact", "host"}:
            data = json.loads(args.json or sys.stdin.read())
            if command == "receive":
                result = engine.receive(SourceInput.model_validate(data))
            elif command == "recall":
                result = engine.recall(RecallRequest.model_validate(data))
            elif command == "correct":
                result = engine.revise(args.id, RevisionInput.model_validate(data))
            elif command == "host":
                from .hosts import handle

                result = handle(engine, args.event, data)
            else:
                from .scheduler import Scheduler
                from .models import ScheduleInput

                result = Scheduler(engine).schedule(ScheduleInput.model_validate(data))
        elif command == "memory":
            from .reading import read_segment

            result = read_segment(
                engine,
                args.id,
                at=args.at,
                known_at=args.known_at,
                offset=args.offset,
                length=args.length,
                budget=args.budget,
                session=args.session,
            )
        elif command in {"backup", "export"}:
            from .transfer import backup, export_records

            result = (
                backup(engine, args.path)
                if command == "backup"
                else export_records(engine, args.path)
            )
        elif command == "api":
            import httpx
            from .api import credential

            with httpx.Client(
                base_url=args.url,
                headers={"Authorization": "Bearer " + credential(engine.db.root)},
            ) as client:
                response = client.request(
                    args.method,
                    args.path,
                    json=json.loads(args.json) if args.json else None,
                )
                response.raise_for_status()
                result = response.json()
        else:
            raise ValueError(command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "benchmark" and not all(result["targets"].values()):
        return 1
    return 0
