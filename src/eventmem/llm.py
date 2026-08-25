"""Anthropic 兼容端点的最小 client（SPEC §3.6）。

只依赖 httpx。key 一律来自环境变量，本文件不含任何真实 key。
base_url 允许带路径前缀（如 https://api.deepseek.com/anthropic），
拼接 /v1/messages 时处理尾斜杠与已含 /v1 的情况。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Final, Iterator

import httpx

__all__ = [
    "ConfigError",
    "LLMError",
    "LLMConfig",
    "LLMClient",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
]


class ConfigError(RuntimeError):
    """配置缺失或非法（护栏层负责降级为纯规则模式）。"""


class LLMError(RuntimeError):
    """请求失败、响应结构异常或 JSON 解析失败。"""


DEFAULT_BASE_URL: Final[str] = "https://api.anthropic.com"
DEFAULT_MODEL: Final[str] = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S: Final[float] = 60.0
ANTHROPIC_VERSION: Final[str] = "2023-06-01"

_RETRY_BACKOFF_S: Final[float] = 2.0
_ERR_BODY_CHARS: Final[int] = 500

# JSON 解析失败后的追加指令（第二次尝试用）
_STRICT_JSON_INSTRUCTION: Final[str] = (
    "\n\nOUTPUT FORMAT (strict): reply with raw JSON only. "
    "No markdown code fences, no explanation, no leading or trailing prose. "
    "The first character of your reply must be '{' or '[' and the last must be '}' or ']'."
)


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量构造；EVENTMEM_API_KEY 缺失抛 ConfigError。"""
        api_key = (os.environ.get("EVENTMEM_API_KEY") or "").strip()
        if not api_key:
            raise ConfigError("EVENTMEM_API_KEY 未设置，LLM 功能不可用")
        base_url = (os.environ.get("EVENTMEM_BASE_URL") or "").strip() or DEFAULT_BASE_URL
        model = (os.environ.get("EVENTMEM_MODEL") or "").strip() or DEFAULT_MODEL
        raw_timeout = (os.environ.get("EVENTMEM_TIMEOUT_S") or "").strip()
        try:
            timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
        except ValueError:  # 非法值按默认处理，不因配置炸掉调用方
            timeout_s = DEFAULT_TIMEOUT_S
        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            timeout_s=timeout_s,
        )

    @property
    def messages_url(self) -> str:
        """{base_url}/v1/messages；base 已以 /v1 结尾时不重复拼。"""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"


# ---------------------------------------------------------------- JSON 提取


def _strip_code_fences(text: str) -> str:
    """取首个 ``` 围栏内的内容；无围栏原样返回。"""
    start = text.find("```")
    if start < 0:
        return text
    # 跳过围栏标记与语言标签所在行
    line_end = text.find("\n", start)
    if line_end < 0:
        return text
    body_start = line_end + 1
    end = text.find("```", body_start)
    return text[body_start:end] if end >= 0 else text[body_start:]


def _scan_balanced(text: str, start: int) -> str | None:
    """从 start 处的 { 或 [ 起扫描配对括号，跳过字符串字面量与转义。"""
    pairs = {"}": "{", "]": "["}
    stack: list[str] = []
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != pairs[ch]:
                return None
            stack.pop()
            if not stack:
                return text[start : i + 1]
    return None


def _iter_candidate_blocks(text: str) -> Iterator[str]:
    """按出现顺序产出所有配对完整的 {} / [] 块。"""
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "{[":
            block = _scan_balanced(text, i)
            if block is not None:
                yield block
                i += len(block)
                continue
        i += 1


def _extract_json_block(text: str) -> str | None:
    """从含围栏／前后杂质的文本中提取首个可解析的 JSON 块，失败返回 None。"""
    if not text:
        return None
    for candidate_text in (_strip_code_fences(text), text):
        for block in _iter_candidate_blocks(candidate_text):
            try:
                json.loads(block)
            except ValueError:
                continue
            return block
    return None


def _parse_json_payload(text: str) -> Any:
    """解析模型输出中的 JSON；解析不出抛 LLMError。"""
    block = _extract_json_block(text)
    if block is None:
        raise LLMError(f"响应中未找到可解析的 JSON：{text[:200]!r}")
    return json.loads(block)


# ---------------------------------------------------------------- client


class LLMClient:
    """Anthropic messages 格式的同步 client。"""

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._http: httpx.Client | None = None

    # -- 生命周期

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.cfg.timeout_s)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 请求

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.cfg.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """POST {base_url}/v1/messages，返回首个文本块内容。

        非 200 或网络异常：退避 2 秒重试 1 次，仍失败抛 LLMError。
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        url = self.cfg.messages_url
        last_error = "未知错误"
        for attempt in range(2):
            if attempt:
                time.sleep(_RETRY_BACKOFF_S)
            try:
                resp = self._client().post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                last_error = f"请求异常：{exc}"
                continue
            if resp.status_code == 200:
                return _text_of(resp)
            last_error = f"HTTP {resp.status_code}：{resp.text[:_ERR_BODY_CHARS]}"
        raise LLMError(f"{url} 调用失败（重试 1 次后）：{last_error}")

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> Any:
        """complete 后解析 JSON；容忍围栏与杂质，解析失败追加严格 JSON 指令重试 1 次。"""
        raw = self.complete(system, user, max_tokens)
        try:
            return _parse_json_payload(raw)
        except LLMError:
            pass
        retry_raw = self.complete(
            system + _STRICT_JSON_INSTRUCTION,
            user + _STRICT_JSON_INSTRUCTION,
            max_tokens,
        )
        return _parse_json_payload(retry_raw)


def _text_of(resp: httpx.Response) -> str:
    """取响应 content 中的文本块（正常情况即 content[0].text）。"""
    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError(f"响应不是 JSON：{exc}") from exc
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list) or not content:
        raise LLMError(f"响应缺少 content：{str(data)[:_ERR_BODY_CHARS]}")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        first = content[0]
        text = first.get("text") if isinstance(first, dict) else None
        if isinstance(text, str):
            return text
        raise LLMError(f"响应 content 无文本块：{str(content)[:_ERR_BODY_CHARS]}")
    return "".join(parts)
