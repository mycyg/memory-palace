"""Run against `eventmem serve`; all example content is synthetic."""

from __future__ import annotations
import argparse, json, tempfile, time
from pathlib import Path
from eventmem.sdk import Client
from eventmem.core.models import SourceInput, Scope, RecallRequest


def run(client, scenario):
    scope = Scope(
        project="memorypalace-example", persona="example", collection=scenario
    )
    namespace = "example:" + scenario
    if scenario == "tool":
        source = client.receive_source(
            SourceInput(
                namespace=namespace,
                key="rollback-rule",
                scope=scope,
                kind="procedure",
                text="Deployment rollback must use the last verified artifact.",
                title="Rollback procedure",
            )
        )
        rid = client.read_source(source_id=source["id"])["record_ids"][0]
        print(
            client.recall(
                RecallRequest(query="deployment rollback", scope=scope, scenario="tool")
            )["text"]
        )
        print(
            client.session_boundary(
                {
                    "event": "checkpoint",
                    "session": "example-tool",
                    "command_id": "checkpoint-example",
                    "scope": scope.model_dump(),
                    "checkpoint": {
                        "goals": ["Deploy the service"],
                        "confirmed_progress": ["Tests passed"],
                        "unverified_results": ["Production health unchecked"],
                        "next_entry": "Check deployment health",
                    },
                }
            )
        )
    elif scenario == "companion":
        source = client.receive_source(
            SourceInput(
                namespace=namespace,
                key="shared-garden",
                scope=scope,
                kind="episode",
                title="A shared afternoon",
                text="We visited the botanical garden on Sunday. The user said she enjoyed the orchids.",
            )
        )
        reminder = client.receive_source(
            SourceInput(
                namespace=namespace,
                key="promise",
                scope=scope,
                kind="commitment",
                text="Follow up on the planned garden visit.",
                title="Garden visit follow-up",
            )
        )
        rid = client.read_source(source_id=reminder["id"])["record_ids"][0]
        print(
            client.create_schedule(
                {
                    "command_id": "example-garden",
                    "policy_id": "example-companion",
                    "record_id": rid,
                    "trigger": "commitment",
                    "due_at": "2030-09-09T10:00:00+08:00",
                }
            )
        )
        print(
            client.recall(
                RecallRequest(query="garden", scope=scope, scenario="companion")
            )["text"]
        )
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manual.md"
            path.write_text(
                "# Service manual\n\nThe request timeout is 60 seconds.\n\n| State | Action |\n|---|---|\n| Failure | Restore verified artifact |\n"
            )
            source = client.upload(
                path,
                SourceInput(
                    namespace=namespace,
                    key="manual",
                    scope=scope,
                    media_type="text/markdown",
                    title="manual.md",
                    authority="document",
                ),
            )
        print(
            json.dumps(
                {"source_id": source["id"], "progress_url": source["read_url"]},
                indent=2,
            )
        )
        for _ in range(120):
            progress = client.read_source(source_id=source["id"])
            if progress["mechanical"] == "complete":
                print(
                    client.recall(
                        RecallRequest(
                            query="request timeout", scope=scope, scenario="knowledge"
                        )
                    )["text"]
                )
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                "Document processing is still pending; inspect its progress URL and worker status"
            )
    return scope


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["tool", "companion", "knowledge"])
    parser.add_argument("--url", default="http://127.0.0.1:8319")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    with Client(args.url, root=args.root) as client:
        run(client, args.scenario)
