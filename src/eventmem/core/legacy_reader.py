"""Read only the historical file-store encoding needed by one-time migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LegacyEvent:
    id: str
    kind: str
    status: str
    intent: str
    body: str
    parent: str | None
    superseded_by: str | None
    outcome: str | None
    lesson: str | None
    anchors: dict
    salience_prior: str | None
    salience_reason: str | None
    prospective: bool


def event_from_markdown(raw: bytes) -> LegacyEvent:
    text = raw.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("Legacy event has no frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("Legacy event has no closing frontmatter fence")
    front = yaml.safe_load("".join(lines[1:end]))
    if not isinstance(front, dict):
        raise TypeError("Legacy event frontmatter is not a mapping")
    for key in ("id", "kind", "status", "intent"):
        if not isinstance(front.get(key), str) or not front[key].strip():
            raise ValueError(f"Legacy event lacks {key}")
    if front["status"] not in {"open", "done", "abandoned", "superseded"}:
        raise ValueError("Legacy event status is invalid")
    anchors = front.get("anchors") or {}
    if not isinstance(anchors, dict):
        raise TypeError("Legacy event anchors are invalid")
    body = "".join(lines[end + 1 :]).removeprefix("\n").removesuffix("\n")
    return LegacyEvent(
        id=front["id"],
        kind=front["kind"],
        status=front["status"],
        intent=front["intent"],
        body=body,
        parent=front.get("parent"),
        superseded_by=front.get("superseded_by"),
        outcome=front.get("outcome"),
        lesson=front.get("lesson"),
        anchors=anchors,
        salience_prior=front.get("salience_prior"),
        salience_reason=front.get("salience_reason"),
        prospective=front.get("prospective") is True,
    )


def archived_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for path in (
        root / "index" / "archive-index.md",
        root / "state" / "archive-index.md",
    ):
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "|" in line and not line.lstrip().startswith("#"):
                    ids.add(line.split("|", 1)[0].strip())
    return ids
