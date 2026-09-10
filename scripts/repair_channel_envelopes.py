"""Review or repair historical channel envelopes through revision APIs.

Requires an explicit scope and --apply for writes. Source snapshots and revision
history remain intact. Generated entries are archived only when their exact
quoted evidence belongs exclusively to the excluded transport header.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import httpx

from eventmem.core.envelopes import current_message


def plan_record(record, raw, body):
    if record["status"] not in {"active", "unverified"}:
        return None
    if record["content"] == raw and not record["generated"]:
        return (
            {"action": "correct", "content": body}
            if body.strip()
            else {"action": "archive"}
        )
    quote = record.get("locator", {}).get("quote")
    if record["generated"] and quote and quote in raw and quote not in body:
        return {"action": "archive"}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8319")
    parser.add_argument("--scope", type=json.loads, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    actions = []
    with httpx.Client(
        base_url=args.url,
        trust_env=False,
        timeout=30,
        headers={
            "Authorization": "Bearer " + (args.root / "local-token").read_text().strip()
        },
    ) as c:

        def get(path, **params):
            r = c.get(path, params=params)
            r.raise_for_status()
            return r.json()

        def record(rid):
            r = get("/v1/memories/" + rid, length=32000, budget=32000)
            while r.get("cursor"):
                part = get(
                    "/v1/memories/" + rid,
                    offset=r["cursor"],
                    length=32000,
                    budget=32000,
                )
                r["content"] += part["content"]
                r["cursor"] = part.get("cursor")
            return r

        sources = set()
        cursor = ""
        while True:
            page = get(
                "/v1/memories",
                **args.scope,
                kind="observation",
                limit=200,
                cursor=cursor,
            )
            for item in page["items"]:
                if current_message(item["content"]) != item["content"]:
                    sources.update(item["source_ids"])
            cursor = page.get("cursor")
            if not cursor:
                break
        for sid in sorted(sources):
            source = get("/v1/sources/" + sid, limit=200)
            if source["scope"] != args.scope or not source["namespace"].startswith(
                "host:"
            ):
                continue
            if source.get("metadata", {}).get("role") != "user":
                continue
            r = c.get("/v1/sources/" + sid + "/content")
            r.raise_for_status()
            raw = r.text
            body = current_message(raw)
            if body == raw:
                continue
            ids = source["record_ids"]
            cursor = source.get("cursor")
            while cursor:
                page = get("/v1/sources/" + sid, limit=200, cursor=cursor)
                ids.extend(page["record_ids"])
                cursor = page.get("cursor")
            for rid in ids:
                item = record(rid)
                if item["source_ids"] != [sid]:
                    continue
                action = plan_record(item, raw, body)
                if action:
                    actions.append(
                        {
                            "id": rid,
                            "source_id": sid,
                            "request": {
                                "expected_revision": item["revision"],
                                "command_id": "channel-repair-"
                                + hashlib.sha256(
                                    (rid + str(item["revision"])).encode()
                                ).hexdigest()[:32],
                                "reason": "Exclude transport history from current user evidence; preserve source and revisions",
                                **action,
                            },
                        }
                    )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"applied": False, "actions": actions}, ensure_ascii=False, indent=2
            )
        )
        args.output.chmod(0o600)
        if args.apply:
            for action in actions:
                r = c.post(
                    "/v1/memories/" + action["id"] + "/revisions",
                    json=action["request"],
                )
                r.raise_for_status()
            args.output.write_text(
                json.dumps(
                    {"applied": True, "actions": actions}, ensure_ascii=False, indent=2
                )
            )
    print(
        json.dumps(
            {
                "applied": args.apply,
                "actions": dict(Counter(a["request"]["action"] for a in actions)),
            }
        )
    )


if __name__ == "__main__":
    main()
