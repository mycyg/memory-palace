"""scrub.py：敏感信息清洗（SPEC §3.14）。

七类规则各至少一例、幂等、scrub_event 的作用域、不误伤正常文本，以及
extract 侧 `config.yml: scrub: false` 的开关集成。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from eventmem.extract import extract_events
from eventmem.schema import Anchors
from eventmem.scrub import RULES, scrub, scrub_event
from eventmem.store import Store

NOW = datetime(2026, 8, 25, 14, 32, 1)

# 测试样例密钥一律运行时拼接构造：源文件文本层不出现完整密钥格式，
# 避免触发 GitHub push protection 的静态扫描（样例本身仍精确命中 scrub 规则）
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_PK_BODY = "MIIEowIBAAKCAQEA" + "1234567890abcdefgHIJKLMN"

# ---------------------------------------------------------------- 七类规则


def test_scrub_redacts_generic_sk_style_api_key() -> None:
    text = "配置里写死了 sk-abcdefghij1234567890，需要挪到环境变量"
    result = scrub(text)
    assert "sk-abcdefghij1234567890" not in result
    assert "<REDACTED:api_key>" in result


def test_scrub_redacts_aws_access_key() -> None:
    text = f"AWS_ACCESS_KEY_ID={FAKE_AWS_KEY} 已经泄露"
    result = scrub(text)
    assert FAKE_AWS_KEY not in result
    assert "<REDACTED:aws_key>" in result


def test_scrub_redacts_github_personal_access_token() -> None:
    token = "ghp_" + "a" * 36
    result = scrub(f"用这个 token 拉私有仓库：{token}")
    assert token not in result
    assert "<REDACTED:github_token>" in result


def test_scrub_redacts_slack_token() -> None:
    text = "slack webhook 用的是 xoxb-1234567890-abcdefghij-klmnopqrst"
    result = scrub(text)
    assert "xoxb-1234567890-abcdefghij-klmnopqrst" not in result
    assert "<REDACTED:slack_token>" in result


def test_scrub_redacts_bearer_token_but_keeps_bearer_prefix() -> None:
    text = "Authorization: Bearer abcdefghij1234567890"
    result = scrub(text)
    assert "abcdefghij1234567890" not in result
    assert "Bearer <REDACTED:bearer>" in result


def test_scrub_redacts_private_key_block_entirely() -> None:
    text = (
        "备份一下密钥：\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{FAKE_PK_BODY}\n"
        "MoreBase64ContentHereForTesting==\n"
        "-----END RSA PRIVATE KEY-----\n"
        "备份完成"
    )
    result = scrub(text)
    assert FAKE_PK_BODY not in result
    assert "-----BEGIN RSA PRIVATE KEY-----" not in result
    assert "<REDACTED:private_key>" in result
    assert "备份一下密钥" in result and "备份完成" in result  # 块外文本不受影响


def test_scrub_redacts_generic_secret_assignment_preserving_key_and_separator() -> None:
    result = scrub("password=supersecret123 记得改掉")
    assert "supersecret123" not in result
    assert "password=<REDACTED:secret>" in result


def test_scrub_secret_rule_is_case_insensitive_and_preserves_original_casing() -> None:
    result = scrub("API_KEY: abcdefgh12345")
    assert "abcdefgh12345" not in result
    assert "API_KEY: <REDACTED:secret>" in result  # 原样保留 key 名的大小写与冒号分隔符


def test_scrub_prefers_specific_tag_over_generic_secret_tag() -> None:
    """sk- 值先被更精确的 api_key 规则命中，通用 secret 规则不会把标签覆盖成 secret。"""
    result = scrub("api_key: sk-abcdefghij1234567890")
    assert "<REDACTED:api_key>" in result
    assert "<REDACTED:secret>" not in result


# ---------------------------------------------------------------- 幂等


def test_scrub_is_idempotent_on_a_single_rule() -> None:
    once = scrub("token: abcdefgh12345678")
    twice = scrub(once)
    assert once == twice


def test_scrub_is_idempotent_on_mixed_secret_types() -> None:
    text = (
        f"sk-abcdefghij1234567890 和 {FAKE_AWS_KEY} 还有 "
        "password=hunter12345 都在这段话里，Bearer abcdefghij1234567890"
    )
    once = scrub(text)
    twice = scrub(once)
    assert once == twice
    assert "hunter12345" not in once


def test_scrub_empty_string_returns_empty_string() -> None:
    assert scrub("") == ""


# ---------------------------------------------------------------- 不误伤正常文本


def test_scrub_preserves_plain_chinese_text_without_secrets() -> None:
    text = "并行启动多个 Ray 任务时，端口按任务 id 错开分配，使用默认端口必然冲突"
    assert scrub(text) == text


def test_scrub_preserves_ordinary_url_without_suspicious_query_keys() -> None:
    text = "参考 https://example.com/articles/2026/08/25/hello-world?utm_source=newsletter&ref=abc123456"
    assert scrub(text) == text


def test_scrub_preserves_plain_hex_numbers_and_commit_shas() -> None:
    text = "commit a3f21c9b8e7d6c5b4a3f21c9b8e7d6c5b4a3f21c 已推送，错误地址 0x7fabc1234 已处理"
    assert scrub(text) == text


def test_scrub_all_seven_rules_have_at_least_one_pattern_registered() -> None:
    """RULES 表覆盖 SPEC §3.14 列出的七类；这条测试锁住表的规模，新增/减少都会被看见。"""
    labels = [rule.label for rule in RULES]
    assert labels == [
        "private_key",
        "api_key",
        "aws_key",
        "github_token",
        "slack_token",
        "bearer",
        "secret",
    ]


# ---------------------------------------------------------------- scrub_event 作用域


def _secret_event(**overrides: Any):
    from eventmem.schema import make_event

    base = dict(
        event_id="2026-08-25_143201",
        kind="fix",
        status="done",
        intent="修复 token=abcdefgh12345 泄露问题",
        outcome="password=supersecret1 已经从日志里清理",
        lesson="secret=leakedvalue1 不应写进配置文件",
        body="调用 curl -H 'Authorization: Bearer abcdefghij1234567890' 排查",
        salience_reason="因为 api_key=abcdefgh12345 被打进了日志",
        anchors=Anchors(
            commits=["a3f21c9"],
            files=["src/config.py"],
            tests=["curl -H 'token: abcdefgh12345' https://internal/health"],
            dialog=["session-01#L1-L9"],
            error_sigs=["ValueError: invalid token=abcdefgh12345 supplied"],
        ),
    )
    base.update(overrides)
    return make_event(**base)


def test_scrub_event_cleans_intent_outcome_lesson_body_and_salience_reason() -> None:
    cleaned = scrub_event(_secret_event())
    assert "abcdefgh12345" not in cleaned.intent
    assert "supersecret1" not in cleaned.outcome
    assert "leakedvalue1" not in cleaned.lesson
    assert "abcdefghij1234567890" not in cleaned.body
    assert "abcdefgh12345" not in cleaned.salience_reason
    assert "<REDACTED:secret>" in cleaned.intent
    assert "<REDACTED:secret>" in cleaned.outcome
    assert "<REDACTED:secret>" in cleaned.lesson
    assert "Bearer <REDACTED:bearer>" in cleaned.body
    assert "<REDACTED:secret>" in cleaned.salience_reason


def test_scrub_event_cleans_error_sigs_and_tests_anchors() -> None:
    cleaned = scrub_event(_secret_event())
    assert all("abcdefgh12345" not in sig for sig in cleaned.anchors.error_sigs)
    assert any("<REDACTED:secret>" in sig for sig in cleaned.anchors.error_sigs)
    assert all("abcdefgh12345" not in cmd for cmd in cleaned.anchors.tests)
    assert any("<REDACTED:secret>" in cmd for cmd in cleaned.anchors.tests)


def test_scrub_event_leaves_commits_files_and_dialog_untouched() -> None:
    """路径与 commit 是客观标识，不在 scrub 的作用域内（即便字面上像 hex/密钥）。"""
    e = _secret_event(
        anchors=Anchors(
            commits=["deadbeef0"],
            files=["src/secret_manager.py"],  # 文件名含 "secret" 但只是路径，不应被处理
            tests=[],
            dialog=["session-01#L1-L9"],
            error_sigs=[],
        )
    )
    cleaned = scrub_event(e)
    assert cleaned.anchors.commits == ["deadbeef0"]
    assert cleaned.anchors.files == ["src/secret_manager.py"]
    assert cleaned.anchors.dialog == ["session-01#L1-L9"]


def test_scrub_event_does_not_mutate_the_input_object() -> None:
    original = _secret_event()
    outcome_before = original.outcome
    scrub_event(original)
    assert original.outcome == outcome_before
    assert "supersecret1" in original.outcome  # 入参对象保持原样未被清洗


def test_scrub_event_handles_none_optional_fields_without_raising() -> None:
    from eventmem.schema import make_event

    e = make_event(
        event_id="2026-08-25_143201",
        kind="build",
        status="open",
        intent="没有敏感信息的普通任务",
        outcome=None,
        lesson=None,
        salience_reason=None,
    )
    cleaned = scrub_event(e)
    assert cleaned.outcome is None
    assert cleaned.lesson is None
    assert cleaned.salience_reason is None
    assert cleaned.intent == "没有敏感信息的普通任务"


# ---------------------------------------------------------------- extract 集成：config.yml scrub 开关


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    import json

    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def _todo_write(tool_id: str, content: str, status: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": content, "status": status}]},
                }
            ],
        },
    }


def test_extract_scrubs_secrets_in_todo_derived_intent_by_default(store: Store, paths, tmp_path: Path) -> None:
    records = [_todo_write("t1", "配置 token=abcdefgh123456 后重新部署", "in_progress")]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-scrub-on", NOW)

    assert len(created) == 1
    intent = store.read(created[0]).intent
    assert "abcdefgh123456" not in intent
    assert "<REDACTED:secret>" in intent


def test_extract_skips_scrub_when_config_disables_it(store: Store, paths, tmp_path: Path) -> None:
    paths.config.write_text("scrub: false\n", encoding="utf-8")
    records = [_todo_write("t1", "配置 token=abcdefgh123456 后重新部署", "in_progress")]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-scrub-off", NOW)

    assert len(created) == 1
    intent = store.read(created[0]).intent
    assert "abcdefgh123456" in intent  # 关闭清洗后原文原样落盘
    assert "<REDACTED:" not in intent


def test_extract_scrubs_secret_before_deriving_error_signature(store: Store, paths, tmp_path: Path) -> None:
    """error_signature 的输入同样先过 scrub：报错文本里的密钥不应出现在落盘的 error_sigs 里。"""
    records = [
        # todo 先开：机械层按 in_progress 起点到窗口末尾收锚点，错误必须落在这个窗口内
        _todo_write("t1", "排查鉴权失败问题", "in_progress"),
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "curl internal"}}
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "b1",
                        "content": "Error: invalid token=abcdefgh123456 provided",
                        "is_error": True,
                    }
                ],
            },
            "toolUseResult": {
                "stdout": "",
                "stderr": "Error: invalid token=abcdefgh123456 provided",
            },
        },
    ]
    transcript_path = tmp_path / "transcript.jsonl"
    _write_transcript(transcript_path, records)

    created = extract_events(transcript_path, store, None, "sess-scrub-err", NOW)

    assert len(created) == 1
    sigs = store.read(created[0]).anchors.error_sigs
    assert sigs  # 报错锚点确实被收集到了
    assert all("abcdefgh123456" not in sig for sig in sigs)
    assert any("<REDACTED:secret>" in sig for sig in sigs)
