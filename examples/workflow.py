"""Synthetic service-backed work memory example.

Start: eventmem serve --root ./example-data
Run:   EVENTMEM_HOME=./example-data uv run python examples/workflow.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from eventmem.sdk import Client


def main() -> None:
    root = Path(os.environ.get("EVENTMEM_HOME", "./example-data"))
    scope = {"project": "memorypalace-example", "collection": "work"}
    url = os.environ.get("EVENTMEM_URL", "http://127.0.0.1:8319")
    with Client(url, root=root) as client:
        source = client.receive_source(
            {
                "namespace": "example",
                "key": "verified-rollback-procedure",
                "occurred_at": "2026-09-01T00:00:00+00:00",
                "scope": scope,
                "kind": "procedure",
                "authority": "operation",
                "title": "Verified rollback procedure",
                "text": "Restore the last verified artifact before changing the service route.",
            }
        )
        boundary = client.session_boundary(
            {
                "session": "example-work-session",
                "scope": scope,
                "event": "checkpoint",
                "command_id": "example-work-checkpoint-" + uuid.uuid4().hex,
                "checkpoint": {
                    "goals": ["Prepare the release"],
                    "confirmed_progress": ["The rollback artifact was verified"],
                    "unverified_results": ["Production health has not been checked"],
                    "next_entry": "Check production health before release",
                },
            }
        )
        recall = client.recall(
            {
                "query": "rollback artifact and release state",
                "scope": scope,
                "scenario": "tool",
                "budget": 800,
            }
        )
    print(f"Source: {source['id']}")
    print(f"Checkpoint: {boundary['status']}")
    print(recall["text"])


if __name__ == "__main__":
    main()
