"""llm.py：纯离线部分——JSON 提取与配置解析。不发任何网络请求。

LLMClient.complete/complete_json 本身需要真实网络，覆盖在 test_integration_llm.py
（@pytest.mark.llm）。这里只测试模块内不依赖网络的纯函数与 LLMConfig.from_env。
"""

from __future__ import annotations

import json

import pytest

import eventmem.llm as llm_mod
from eventmem.llm import ConfigError, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TIMEOUT_S, LLMConfig

_ENV_KEYS = ("EVENTMEM_API_KEY", "EVENTMEM_BASE_URL", "EVENTMEM_MODEL", "EVENTMEM_TIMEOUT_S")


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试开始前清空相关环境变量，不受运行机器上真实 .env / 导出变量影响。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------- _extract_json_block


def test_extract_json_block_strips_code_fence() -> None:
    text = '```json\n{"a": 1, "b": [1, 2, 3]}\n```'
    block = llm_mod._extract_json_block(text)
    assert block is not None
    assert "```" not in block
    assert json.loads(block) == {"a": 1, "b": [1, 2, 3]}


def test_extract_json_block_ignores_leading_and_trailing_prose() -> None:
    text = "Sure! Here's the JSON you asked for:\n{\"a\": 1}\nHope that helps!"
    block = llm_mod._extract_json_block(text)
    assert block is not None
    assert json.loads(block) == {"a": 1}


def test_extract_json_block_handles_nested_brackets_and_braces_in_strings() -> None:
    text = '{"a": {"b": [1, 2, 3]}, "c": "text with } brace and ] bracket inside string"}'
    block = llm_mod._extract_json_block(text)
    assert block is not None
    parsed = json.loads(block)
    assert parsed == {
        "a": {"b": [1, 2, 3]},
        "c": "text with } brace and ] bracket inside string",
    }


def test_extract_json_block_handles_escaped_quotes_in_strings() -> None:
    text = '{"a": "she said \\"hi\\" then left"}'
    block = llm_mod._extract_json_block(text)
    assert block is not None
    assert json.loads(block) == {"a": 'she said "hi" then left'}


def test_extract_json_block_returns_none_when_no_json_present() -> None:
    assert llm_mod._extract_json_block("no json here at all, sorry") is None


def test_extract_json_block_returns_none_for_unclosed_braces() -> None:
    assert llm_mod._extract_json_block("{unclosed") is None


def test_extract_json_block_returns_none_for_empty_string() -> None:
    assert llm_mod._extract_json_block("") is None


def test_extract_json_block_finds_top_level_array() -> None:
    text = 'prefix text [1, 2, {"x": "a}b"}, 3] suffix text'
    block = llm_mod._extract_json_block(text)
    assert block is not None
    assert json.loads(block) == [1, 2, {"x": "a}b"}, 3]


# ---------------------------------------------------------------- LLMConfig.from_env


def test_from_env_raises_config_error_when_api_key_missing() -> None:
    with pytest.raises(ConfigError):
        LLMConfig.from_env()


def test_from_env_uses_defaults_when_only_api_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-test-fake")
    cfg = LLMConfig.from_env()
    assert cfg.api_key == "sk-test-fake"
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.model == DEFAULT_MODEL
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S


def test_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-test-fake")
    monkeypatch.setenv("EVENTMEM_BASE_URL", "https://api.deepseek.com/anthropic/")
    monkeypatch.setenv("EVENTMEM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("EVENTMEM_TIMEOUT_S", "30")
    cfg = LLMConfig.from_env()
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.timeout_s == 30.0
    # base_url 存的是去掉尾斜杠后的形式；messages_url 正确拼接 /v1/messages
    assert cfg.base_url == "https://api.deepseek.com/anthropic"
    assert cfg.messages_url == "https://api.deepseek.com/anthropic/v1/messages"


def test_from_env_falls_back_to_default_timeout_on_illegal_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-test-fake")
    monkeypatch.setenv("EVENTMEM_TIMEOUT_S", "not-a-number")
    cfg = LLMConfig.from_env()
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S


def test_from_env_treats_blank_api_key_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVENTMEM_API_KEY", "   ")
    with pytest.raises(ConfigError):
        LLMConfig.from_env()


def test_messages_url_does_not_duplicate_v1_when_base_already_ends_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTMEM_API_KEY", "sk-test-fake")
    monkeypatch.setenv("EVENTMEM_BASE_URL", "https://example.com/v1")
    cfg = LLMConfig.from_env()
    assert cfg.messages_url == "https://example.com/v1/messages"
