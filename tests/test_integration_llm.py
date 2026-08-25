"""集成测试：需要真实 Anthropic 兼容端点。

CI／默认 `pytest` 运行会跳过（pyproject.toml 的 `addopts = "-m 'not llm'"`）；
显式运行：

    .venv/bin/python -m pytest -m llm tests/test_integration_llm.py

读取 EVENTMEM_BASE_URL / EVENTMEM_API_KEY / EVENTMEM_MODEL 三个环境变量，任意一个
缺失都 pytest.skip（而不是失败）——缺失时静默退回默认端点会打到非预期的服务，
这里要求显式配置齐全才跑。每个测试限制在一到两次请求以内，控制成本。
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from eventmem.consolidate import light
from eventmem.extract import extract_events
from eventmem.index import Budget
from eventmem.llm import LLMClient, LLMConfig
from eventmem.store import Store

pytestmark = pytest.mark.llm

_REQUIRED_ENV_VARS = ("EVENTMEM_BASE_URL", "EVENTMEM_API_KEY", "EVENTMEM_MODEL")


def _require_llm_config() -> LLMConfig:
    missing = [name for name in _REQUIRED_ENV_VARS if not (os.environ.get(name) or "").strip()]
    if missing:
        pytest.skip(f"跳过真实 LLM 集成测试：缺少环境变量 {', '.join(missing)}")
    return LLMConfig.from_env()


@pytest.fixture
def llm_client():
    cfg = _require_llm_config()
    client = LLMClient(cfg)
    yield client
    client.close()


def test_complete_json_round_trips_a_small_json_task(llm_client: LLMClient) -> None:
    """单次请求：真实端点返回的 JSON 能被 complete_json 正确解析。"""
    result = llm_client.complete_json(
        system="You output raw JSON only. No prose, no markdown code fence.",
        user='Reply with exactly this JSON object and nothing else: {"ok": true, "value": 7}',
        max_tokens=200,
    )
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result.get("value") == 7


def test_extract_events_with_real_client_respects_cap_and_required_fields(
    llm_client: LLMClient, store: Store, paths, tmp_path
) -> None:
    """单次请求：合成 transcript 跑真实抽取，断言事件数不超上限、必填字段齐全。"""
    transcript_path = tmp_path / "transcript.jsonl"
    lines = [
        (
            '{"type": "user", "message": {"role": "user", "content": '
            '"我们决定用方案 A 而不是方案 B，因为 B 需要额外的外部依赖，长期维护成本更高"}}'
        ),
        (
            '{"type": "assistant", "message": {"role": "assistant", "content": '
            '[{"type": "text", "text": "好的，已经确认选用方案 A，后续按这个方向推进实现"}]}}'
        ),
    ]
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    created = extract_events(transcript_path, store, llm_client, "sess-integration", datetime.now())

    assert len(created) <= 20  # SPEC §3.7：一次会话产出事件数上限 20
    for event_id in created:
        event = store.read(event_id)
        assert event.intent.strip() != ""
        assert event.kind in ("decision", "build", "explore", "fix")
        assert event.status in ("open", "done", "abandoned")
        if event.status != "open":
            assert (event.outcome or "").strip() != ""


def test_light_fills_outcome_with_real_client(llm_client: LLMClient, store: Store, paths, event_factory) -> None:
    """单次请求：已闭合但缺 outcome 的事件，走真实 LLM 补写一句结论。"""
    event_id = store.append(
        event_factory(
            status="abandoned",
            outcome=None,
            intent="尝试引入向量检索作为兜底召回",
            body="调研了两个候选库\n因为运维复杂度超过收益而放弃",
        )
    )

    light(store, paths, Budget(), llm_client, datetime.now())

    result = store.read(event_id)
    assert (result.outcome or "").strip() != ""
    assert result.intent == "尝试引入向量检索作为兜底召回"  # 不可变纪律：补写 outcome 不应动 intent
