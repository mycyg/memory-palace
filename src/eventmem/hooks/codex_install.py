"""Install project/user Codex hooks while preserving unrelated handlers."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from eventmem.core.models import Scope
from eventmem.hooks.codex import EVENTS
from eventmem.paths import atomic_write

MODULE = "eventmem.hooks.codex"


def owned(handler):
    try:
        args = shlex.split(handler.get("command", ""))
    except ValueError:
        return False
    return any(args[i : i + 2] == ["-m", MODULE] for i in range(len(args) - 1))


def install(config_dir, *, root, url, scope=None, scenario="tool", remove=False):
    config_dir = Path(config_dir).expanduser().resolve()
    path = config_dir / "hooks.json"
    previous = path.read_text() if path.exists() else None
    document = json.loads(previous) if previous else {}
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError("Existing hooks.json must contain a hooks object")
    command = [
        sys.executable,
        "-m",
        MODULE,
        "--root",
        str(Path(root).expanduser().resolve()),
        "--url",
        url,
        "--scenario",
        scenario,
    ]
    if scope is not None:
        command += ["--scope", Scope.model_validate(scope).model_dump_json()]
    for name in dict.fromkeys([*hooks, *EVENTS]):
        kept = []
        for group in hooks.get(name, []):
            handlers = [
                handler for handler in group.get("hooks", []) if not owned(handler)
            ]
            if handlers:
                kept.append({**group, "hooks": handlers})
        if not remove and name in EVENTS:
            kept.append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": shlex.join(command),
                            "timeout": 3 if name in {"SessionEnd", "Interrupt"} else 8,
                            "statusMessage": "MemoryPalace",
                        }
                    ]
                }
            )
        if kept:
            hooks[name] = kept
        else:
            hooks.pop(name, None)
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    changed = content != previous
    if changed:
        config_dir.mkdir(parents=True, exist_ok=True)
        if previous is not None:
            atomic_write(path.with_suffix(".json.memorypalace-backup"), previous)
        atomic_write(path, content)
    return {
        "path": str(path),
        "changed": changed,
        "events": [] if remove else list(EVENTS),
        "next": "Restart Codex and review/trust the MemoryPalace definitions with /hooks."
        if not remove
        else "Restart Codex to unload the removed hooks.",
    }
