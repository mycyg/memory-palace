"""敏感信息清洗（SPEC §3.14）。

位置：extract 构造事件之后、store.append 之前是唯一入口；error_signature 的输入
文本同样先过一遍。L0 不可删除，写进去的密钥就永久留在磁盘上，因此清洗必须发生在
写入之前而不是读取之后。

规则表 `RULES` 是模块级常量，新增一类只需往表里加一条 `Rule`。所有替换结果都是
`<REDACTED:类型>` 形态，且没有任何规则能匹配这个形态，故 `scrub(scrub(x)) == scrub(x)`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

from .schema import Anchors, Event

__all__ = ["RULES", "Rule", "scrub", "scrub_event"]


@dataclass(frozen=True)
class Rule:
    """一类敏感信息：`pattern` 命中的片段整体替换为 `repl`。

    `repl` 里可以用 `\\g<name>` 回引分组，用于保留 key 名而只替换值。
    """

    label: str
    pattern: re.Pattern[str]
    repl: str


def _tag(label: str) -> str:
    return f"<REDACTED:{label}>"


# 顺序有意义：先整块的私钥，再各家专有前缀的令牌，最后才是通用的 key=value 兜底，
# 避免通用规则把更精确的类型标签盖掉。
RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        _tag("private_key"),
    ),
    Rule("api_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}"), _tag("api_key")),
    Rule("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), _tag("aws_key")),
    Rule("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}"), _tag("github_token")),
    Rule("slack_token", re.compile(r"xox[abp]-[A-Za-z0-9-]{10,}"), _tag("slack_token")),
    Rule("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"), f"Bearer {_tag('bearer')}"),
    # 保留 key 名与分隔符，只替换值；值已是占位符时不再处理（幂等的关键）
    Rule(
        "secret",
        re.compile(
            r"(?P<key>password|passwd|secret|token|api_key)(?P<sep>\s*[=:]\s*)(?!<REDACTED:)\S{8,}",
            re.IGNORECASE,
        ),
        f"\\g<key>\\g<sep>{_tag('secret')}",
    ),
)


def scrub(text: str) -> str:
    """按 RULES 逐条替换；幂等（对已替换过的文本再跑一次结果不变）。"""
    if not text:
        return text
    result = text
    for rule in RULES:
        result = rule.pattern.sub(rule.repl, result)
    return result


def _scrub_opt(text: str | None) -> str | None:
    """可空字段：None 保持 None。"""
    return None if text is None else scrub(text)


def scrub_event(e: Event) -> Event:
    """清洗事件的自由文本字段，返回新对象；入参不被修改。

    作用域按 SPEC §3.14 并扩两项（集成裁决）：intent / outcome / lesson / body /
    salience_reason / anchors.error_sigs / anchors.tests——reason 是 LLM 自由文本、
    tests 是 shell 命令原文（可能含 `curl -H "Authorization: ..."`），都可能带出密钥。
    路径与 commit 是客观标识，不清洗。
    """
    anchors = Anchors(
        commits=list(e.anchors.commits),
        files=list(e.anchors.files),
        tests=[scrub(cmd) for cmd in e.anchors.tests],
        dialog=list(e.anchors.dialog),
        error_sigs=[scrub(sig) for sig in e.anchors.error_sigs],
    )
    return replace(
        e,
        intent=scrub(e.intent),
        outcome=_scrub_opt(e.outcome),
        lesson=_scrub_opt(e.lesson),
        body=scrub(e.body),
        salience_reason=_scrub_opt(e.salience_reason),
        anchors=anchors,
    )
