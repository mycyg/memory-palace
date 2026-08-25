"""事件模型与 markdown（yaml frontmatter ＋ 正文）序列化。

磁盘格式：

    ---
    <yaml frontmatter>
    ---
    <正文：行动序列摘要，可为空>

写入约定：正文非空时在闭合分隔线后空一行、结尾补一个换行；解析时对称地剥掉这
一个前导换行与一个尾随换行，因此 `from_markdown(to_markdown(e)) == e` 恒成立。

v0.2 扩展字段（salience_prior / salience_reason / prospective，SPEC §3.11 §3.12）
取默认值时不写进 frontmatter：旧事件文件被重写后 diff 保持最小，旧文件缺这些字段
也按默认值解析，两个方向都向后兼容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import yaml

Kind = Literal["decision", "build", "explore", "fix"]
Status = Literal["open", "done", "abandoned", "superseded"]
SaliencePrior = Literal["low", "medium", "high"]

# 状态是闭合枚举（DESIGN §2.4 状态机）；kind 是开放枚举，不做取值校验
STATUSES: tuple[str, ...] = ("open", "done", "abandoned", "superseded")
# 显著性先验也是闭合枚举（SPEC §3.11）；非法取值按缺失处理，见 _opt_prior
SALIENCE_PRIORS: tuple[str, ...] = ("low", "medium", "high")

# yaml 里被视为真的标量写法（PyYAML 已把 true/yes 解析成 bool，这里兜住字符串形态）
_TRUE_TOKENS: frozenset[str] = frozenset({"true", "yes", "on", "1"})

_FENCE = "---"
_ID_FORMAT = "%Y-%m-%d_%H%M%S"


class SchemaError(ValueError):
    """事件文本不符合 schema（缺必填字段、frontmatter 损坏、状态非法）。"""


@dataclass
class Anchors:
    """客观锚点：检索与审计用，不含过程原文。"""

    commits: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    dialog: list[str] = field(default_factory=list)
    error_sigs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """五类锚点是否都为空。"""
        return not (self.commits or self.files or self.tests or self.dialog or self.error_sigs)


@dataclass
class Event:
    """一个闭环：意图 → 行动 → 结果。写入后 intent/body 不再修改。

    前十个字段的顺序与 SPEC §3.2 逐字一致，且一律不带默认值：构造时必须写全，
    按位置或按关键字构造都不会错位。其后是 v0.2 扩展字段（SPEC §3.11 §3.12），
    带默认值以兼容既有的十字段构造点。
    """

    id: str
    parent: str | None
    kind: Kind
    status: Status
    superseded_by: str | None
    intent: str
    anchors: Anchors
    outcome: str | None
    lesson: str | None
    body: str
    salience_prior: SaliencePrior | None = None  # 闭合时的自评档位
    salience_reason: str | None = None  # 自评的一句理由
    prospective: bool = False  # 前瞻标记：来自将来时意图的 open 事件

    def is_open(self) -> bool:
        """事件是否尚未闭合。"""
        return self.status == "open"


class _EventDumper(yaml.SafeDumper):
    """frontmatter 专用 dumper：多行字符串走 literal block，可读且不丢换行。"""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """多行字符串用 | 块标量；含行尾空白等无法块化的内容由 PyYAML 自动回退到引号形式。"""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_EventDumper.add_representer(str, _represent_str)


def make_event(
    event_id: str,
    kind: Kind,
    status: Status,
    intent: str,
    *,
    parent: str | None = None,
    superseded_by: str | None = None,
    anchors: Anchors | None = None,
    outcome: str | None = None,
    lesson: str | None = None,
    body: str = "",
    salience_prior: SaliencePrior | None = None,
    salience_reason: str | None = None,
    prospective: bool = False,
) -> Event:
    """构造事件的便利入口：只写必填字段，其余取空值；Event 本身保持全字段必填。"""
    return Event(
        id=event_id,
        parent=parent,
        kind=kind,
        status=status,
        superseded_by=superseded_by,
        intent=intent,
        anchors=anchors if anchors is not None else Anchors(),
        outcome=outcome,
        lesson=lesson,
        body=body,
        salience_prior=salience_prior,
        salience_reason=salience_reason,
        prospective=prospective,
    )


def new_id(now: datetime, existing: set[str]) -> str:
    """按时间戳生成事件 id；与已有 id 冲突时追加 -2、-3 后缀。"""
    base = now.strftime(_ID_FORMAT)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def id_to_datetime(event_id: str) -> datetime | None:
    """从事件 id 前缀取时间；无法解析返回 None。"""
    head = event_id.split("-", 3)
    if len(head) < 3:
        return None
    stamp = "-".join(head[:3])
    try:
        return datetime.strptime(stamp, _ID_FORMAT)
    except ValueError:
        return None


def to_markdown(e: Event) -> str:
    """把事件序列化为 frontmatter ＋ 正文的 markdown 文本。"""
    front: dict[str, Any] = {
        "id": e.id,
        "parent": e.parent,
        "kind": e.kind,
        "status": e.status,
        "superseded_by": e.superseded_by,
        "intent": e.intent,
        "anchors": {
            "commits": list(e.anchors.commits),
            "files": list(e.anchors.files),
            "tests": list(e.anchors.tests),
            "dialog": list(e.anchors.dialog),
            "error_sigs": list(e.anchors.error_sigs),
        },
        "outcome": e.outcome,
        "lesson": e.lesson,
    }
    # v0.2 扩展字段取默认值时整条省略：旧事件重写后 frontmatter 逐字不变
    if e.salience_prior is not None:
        front["salience_prior"] = e.salience_prior
    if e.salience_reason is not None:
        front["salience_reason"] = e.salience_reason
    if e.prospective:
        front["prospective"] = True
    dumped = yaml.dump(
        front,
        Dumper=_EventDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    head = f"{_FENCE}\n{dumped}{_FENCE}\n"
    if e.body == "":
        return head
    return f"{head}\n{e.body}\n"


def from_markdown(text: str) -> Event:
    """解析事件文本；缺 id/intent/kind/status 或状态非法则 raise SchemaError。"""
    front, body = _split_frontmatter(text)
    event_id = _require_str(front, "id")
    kind = _require_str(front, "kind")
    status = _require_str(front, "status")
    intent = _require_str(front, "intent")
    if status not in STATUSES:
        raise SchemaError(f"事件 {event_id} 的 status 非法：{status}")
    return Event(
        id=event_id,
        kind=kind,  # type: ignore[arg-type]  # kind 为开放枚举，不做取值校验
        status=status,  # type: ignore[arg-type]
        intent=intent,
        parent=_opt_str(front.get("parent")),
        superseded_by=_opt_str(front.get("superseded_by")),
        anchors=_parse_anchors(front.get("anchors")),
        outcome=_opt_str(front.get("outcome")),
        lesson=_opt_str(front.get("lesson")),
        body=body,
        salience_prior=_opt_prior(front.get("salience_prior")),
        salience_reason=_opt_str(front.get("salience_reason")),
        prospective=_as_bool(front.get("prospective")),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """切出 frontmatter 字典与正文；正文剥掉写入时补的一个前导换行与一个尾随换行。"""
    normalized = text.lstrip("﻿")
    if not normalized.startswith(_FENCE):
        raise SchemaError("事件文本缺少 frontmatter 起始分隔线")
    rest = normalized[len(_FENCE) :]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = _find_fence(rest)
    if end is None:
        raise SchemaError("事件文本缺少 frontmatter 结束分隔线")
    raw_front, tail = rest[:end], rest[end + len(_FENCE) :]
    try:
        loaded = yaml.safe_load(raw_front)
    except yaml.YAMLError as exc:
        raise SchemaError(f"frontmatter 不是合法 yaml：{exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise SchemaError("frontmatter 顶层必须是映射")
    body = tail
    if body.startswith("\n"):  # 结束分隔线自身的换行
        body = body[1:]
    if body.startswith("\n"):  # 写入时补的空行
        body = body[1:]
    if body.endswith("\n"):  # 写入时补的结尾换行
        body = body[:-1]
    return loaded, body


def _find_fence(text: str) -> int | None:
    """找到独占一行的结束分隔线位置，返回其起始下标。"""
    pos = 0
    while pos <= len(text):
        if text.startswith(_FENCE, pos):
            tail = text[pos + len(_FENCE) :]
            if tail == "" or tail.startswith("\n"):
                return pos
        nxt = text.find("\n", pos)
        if nxt == -1:
            return None
        pos = nxt + 1
    return None


def _require_str(front: dict[str, Any], key: str) -> str:
    """取必填字符串字段；缺失、None 或空白视为缺失。"""
    value = front.get(key)
    if value is None:
        raise SchemaError(f"frontmatter 缺少必填字段 {key}")
    text = str(value)
    if not text.strip():
        raise SchemaError(f"frontmatter 字段 {key} 为空")
    return text


def _opt_str(value: Any) -> str | None:
    """可空字符串字段：None 保持 None，其余转字符串（空串保持空串）。"""
    if value is None:
        return None
    return str(value)


def _opt_prior(value: Any) -> SaliencePrior | None:
    """显著性先验：缺失或非枚举取值一律按缺失处理。

    不 raise：这是一个可选的派生性字段，取值写坏不该让整个事件文件读不出来。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in SALIENCE_PRIORS:
        return text  # type: ignore[return-value]
    return None


def _as_bool(value: Any) -> bool:
    """布尔字段：缺失即 False；兼容 yaml 已解析的 bool 与字符串形态。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_TOKENS


def _parse_anchors(value: Any) -> Anchors:
    """解析 anchors 子映射；缺字段补空列表，标量宽容地包成单元素列表。"""
    if value is None:
        return Anchors()
    if not isinstance(value, dict):
        raise SchemaError("anchors 必须是映射")
    return Anchors(
        commits=_str_list(value.get("commits")),
        files=_str_list(value.get("files")),
        tests=_str_list(value.get("tests")),
        dialog=_str_list(value.get("dialog")),
        error_sigs=_str_list(value.get("error_sigs")),
    )


def _str_list(value: Any) -> list[str]:
    """把 yaml 取值规约成字符串列表，None 得空列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]
