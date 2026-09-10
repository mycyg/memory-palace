import json
import shlex
import subprocess
import sys

import pytest

from eventmem.core import Engine, SourceInput
from eventmem.core.hosts import handle
from eventmem.core.jobs import Worker
from eventmem.hooks import codex
from eventmem.hooks.codex_install import install


def test_native_prompt_reply_and_compaction(tmp_path, monkeypatch):
    engine = Engine(tmp_path)
    monkeypatch.setattr(
        codex,
        "deliver",
        lambda event, payload, **kwargs: handle(engine, event, payload),
    )
    engine.receive(
        SourceInput(
            namespace="fixture",
            key="fact",
            text="The companion prefers jasmine tea.",
            kind="preference",
        )
    )
    base = {"session_id": "codex-live", "cwd": str(tmp_path), "turn_id": "turn-1"}
    options = {
        "root": tmp_path,
        "url": "http://unused",
        "scope": {"project": "personal"},
        "scenario": "companion",
    }
    prompt = base | {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Which jasmine tea did we discuss?",
    }
    output = codex.run(prompt, **options)
    assert "prefers jasmine tea" in output["hookSpecificOutput"]["additionalContext"]
    assert "Which jasmine tea" not in output["hookSpecificOutput"]["additionalContext"]
    before = engine.overview()["sources"]
    codex.run(prompt, **options)
    assert engine.overview()["sources"] == before
    reply = base | {
        "hook_event_name": "Stop",
        "last_assistant_message": "I think you enjoy jasmine tea.",
    }
    assert codex.run(reply, **options) == {}
    assert codex.run(reply, **options) == {}
    with engine.db.connect() as conn:
        sources = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM sources WHERE namespace='host:codex'"
            )
        ]
    assert sorted(s["authority"] for s in sources) == ["explicit", "model"]
    assert {s["metadata"]["role"] for s in sources} == {"user", "assistant"}
    assert (
        codex.run(
            base | {"hook_event_name": "PreCompact", "trigger": "auto"}, **options
        )
        == {}
    )
    restored = codex.run(
        base | {"hook_event_name": "SessionStart", "source": "compact"}, **options
    )
    assert "prefers jasmine tea" in restored["hookSpecificOutput"]["additionalContext"]


def test_normalization_excludes_transcripts_and_memory_tools(tmp_path):
    base = {
        "session_id": "codex",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
        "turn_id": "t",
        "tool_use_id": "c",
        "tool_name": "Bash",
        "tool_input": {"command": "cat module.py"},
        "tool_response": "source",
        "transcript_path": "/private/rollout.jsonl",
    }
    event, payload = codex.normalize(base)
    assert event == "tool" and payload["host"] == "codex"
    assert "transcript_path" not in payload
    assert (
        codex.normalize(base | {"tool_name": "mcp__memorypalace__recall_memory"})
        is None
    )
    assert codex.normalize(base | {"hook_event_name": "Unknown"}) is None
    assert codex.normalize(base | {"session_id": ""}) is None
    assert (
        codex.normalize(
            base | {"hook_event_name": "Stop", "last_assistant_message": None}
        )
        is None
    )
    second = codex.normalize(base | {"tool_use_id": "next"})[1]
    assert second["command_id"] != payload["command_id"]


def test_offline_spool_replays_without_echo_or_duplicate(tmp_path):
    engine = Engine(tmp_path)
    raw = {
        "session_id": "offline-codex",
        "turn_id": "t",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Remember the blue notebook.",
    }
    opts = {
        "root": tmp_path,
        "url": "http://127.0.0.1:1",
        "scope": {"project": "personal"},
    }
    assert codex.run(raw, **opts) == {}
    assert codex.run(raw, **opts) == {}
    assert len(list((tmp_path / "host-spool").glob("*.json"))) == 1
    Worker(engine).replay_hosts()
    Worker(engine).replay_hosts()
    assert engine.overview()["sources"] == 1
    with engine.db.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM sessions WHERE id='offline-codex'"
        ).fetchone()
    assert not list((tmp_path / "host-spool").glob("*.json"))


def test_hook_http_transport_receipts(service):
    engine, url = service
    payload = {
        "session_id": "wire",
        "turn_id": "t",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Remember this native hook.",
    }
    assert codex.run(payload, root=engine.db.root, url=url) == {}
    assert engine.overview()["sources"] == 1
    assert not list((engine.db.root / "host-spool").glob("*.json"))


@pytest.fixture
def service(tmp_path):
    # Exercise a real authenticated HTTP service in the same way as other hosts.
    from test_protocols_v1 import service as shared_service

    yield from shared_service.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "c",
                "last_assistant_message": "A final reply",
            }
        ),
    ],
)
def test_hook_cli_returns_json_and_never_blocks(tmp_path, raw):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eventmem.hooks.codex",
            "--root",
            str(tmp_path),
            "--url",
            "http://127.0.0.1:1",
        ],
        input=raw,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {}
    assert "final reply" not in result.stderr


def test_install_preserves_other_hooks_and_roundtrips(tmp_path):
    directory = tmp_path / "project with spaces" / ".codex"
    directory.mkdir(parents=True)
    unrelated = {"type": "command", "command": "python policy.py"}
    original = {
        "description": "Keep metadata",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [unrelated]}]},
    }
    (directory / "hooks.json").write_text(json.dumps(original))
    opts = {
        "root": tmp_path / "private $data",
        "url": "http://127.0.0.1:8319",
        "scope": {"persona": "companion"},
        "scenario": "companion",
    }
    assert install(directory, **opts)["changed"]
    content = json.loads((directory / "hooks.json").read_text())
    assert content["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
    assert set(content["hooks"]) == set(codex.EVENTS)
    args = shlex.split(content["hooks"]["Stop"][0]["hooks"][0]["command"])
    assert args[args.index("--root") + 1] == str(opts["root"])
    assert not install(directory, **opts)["changed"]
    install(directory, **opts, remove=True)
    assert json.loads((directory / "hooks.json").read_text()) == original


def test_install_cli_has_no_database_side_effect(tmp_path):
    root, project = tmp_path / "memory", tmp_path / "project"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eventmem.cli",
            "codex",
            "install",
            "--project",
            str(project),
            "--root",
            str(root),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["changed"]
    assert (project / ".codex/hooks.json").is_file()
    assert not root.exists()
