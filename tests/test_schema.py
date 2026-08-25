"""schema.py：事件模型与 markdown 序列化。

核心纪律：`from_markdown(to_markdown(e)) == e` 必须对各种正文形态恒成立
（多行、含中文、含行尾空白、字段为 None），且缺必填字段要 raise SchemaError。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from eventmem.schema import (
    Anchors,
    SchemaError,
    from_markdown,
    id_to_datetime,
    new_id,
    to_markdown,
)

# ---------------------------------------------------------------- 往返恒等


def test_round_trip_simple_single_line_event(event_factory) -> None:
    e = event_factory(
        kind="fix",
        status="done",
        intent="修复端口冲突",
        anchors=Anchors(commits=["a3f21c9"], files=["train/launcher.py"]),
        outcome="端口冲突已消除",
    )
    assert from_markdown(to_markdown(e)) == e


def test_round_trip_multiline_intent_and_body(event_factory) -> None:
    e = event_factory(
        kind="decision",
        status="done",
        intent="第一行意图\n第二行补充说明\n第三行",
        outcome="结论也\n跨多行",
        body="行动一\n行动二\n行动三",
    )
    assert from_markdown(to_markdown(e)) == e


def test_round_trip_none_optional_fields(event_factory) -> None:
    e = event_factory(
        parent=None,
        superseded_by=None,
        outcome=None,
        lesson=None,
        body="",
    )
    md = to_markdown(e)
    e2 = from_markdown(md)
    assert e2 == e
    assert e2.parent is None
    assert e2.superseded_by is None
    assert e2.outcome is None
    assert e2.lesson is None


def test_round_trip_chinese_text_all_fields(event_factory) -> None:
    e = event_factory(
        parent="2026-08-24_090000",
        kind="explore",
        status="abandoned",
        intent="尝试用向量库做兜底检索，评估召回质量",
        anchors=Anchors(
            commits=["a3f21c9", "b7e0011"],
            files=["src/召回.py", "配置/参数.yml"],
            tests=["pytest tests/test_召回.py"],
            dialog=["session-0825#L10-L88"],
            error_sigs=["ValueError: 向量维度不匹配"],
        ),
        outcome="放弃：该规模下 BM25 已经够用，向量库引入的复杂度不值得",
        lesson="小规模检索不需要引入向量库",
        body="调研三个候选库\n跑通一个 demo\n评估后放弃",
    )
    assert from_markdown(to_markdown(e)) == e


def test_round_trip_trailing_whitespace_in_body_lines(event_factory) -> None:
    """body 不经过 YAML，行尾空白应逐字保留。"""
    e = event_factory(
        intent="行尾空白测试",
        status="done",
        outcome="完成",
        body="第一行\n第二行 trailing space   \n第三行",
    )
    e2 = from_markdown(to_markdown(e))
    assert e2 == e
    assert e2.body == "第一行\n第二行 trailing space   \n第三行"


def test_round_trip_trailing_whitespace_in_intent_field(event_factory) -> None:
    """intent 是 frontmatter 字段：含行尾空白时 PyYAML 会回退到引号风格而非块标量，
    但往返恒等仍必须成立。"""
    e = event_factory(intent="多行意图\n第二行 trailing   ", status="open")
    md = to_markdown(e)
    assert from_markdown(md) == e


def test_round_trip_empty_body_produces_no_trailing_body_block(event_factory) -> None:
    e = event_factory(body="")
    md = to_markdown(e)
    # body 为空串时不应画蛇添足地留出空的正文块
    assert md.endswith("---\n")
    assert from_markdown(md).body == ""


# ---------------------------------------------------------------- 缺字段校验


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("---\nparent: null\nkind: fix\nstatus: open\nintent: x\n---\n", id="missing_id"),
        pytest.param("---\nid: a\nstatus: open\nintent: x\n---\n", id="missing_kind"),
        pytest.param("---\nid: a\nkind: fix\nintent: x\n---\n", id="missing_status"),
        pytest.param("---\nid: a\nkind: fix\nstatus: open\n---\n", id="missing_intent"),
        pytest.param("---\nid: a\nkind: fix\nstatus: open\nintent: '   '\n---\n", id="blank_intent"),
        pytest.param("---\nid: a\nkind: fix\nstatus: bogus\nintent: x\n---\n", id="illegal_status"),
    ],
)
def test_from_markdown_missing_required_field_raises_schema_error(raw: str) -> None:
    with pytest.raises(SchemaError):
        from_markdown(raw)


def test_from_markdown_missing_frontmatter_fence_raises_schema_error() -> None:
    with pytest.raises(SchemaError):
        from_markdown("id: a\nkind: fix\nstatus: open\nintent: x\n")


def test_from_markdown_accepts_arbitrary_kind_open_enum() -> None:
    """kind 是开放枚举（DESIGN §2.3），from_markdown 不应校验取值。"""
    e = from_markdown("---\nid: a\nkind: totally_custom_kind\nstatus: open\nintent: x\n---\n")
    assert e.kind == "totally_custom_kind"


def test_from_markdown_missing_anchors_defaults_to_empty() -> None:
    e = from_markdown("---\nid: a\nkind: fix\nstatus: open\nintent: x\n---\n")
    assert e.anchors == Anchors()
    assert e.anchors.is_empty()


# ---------------------------------------------------------------- new_id


def test_new_id_uses_plain_timestamp_when_no_conflict() -> None:
    now = datetime(2026, 8, 25, 14, 32, 1)
    assert new_id(now, existing=set()) == "2026-08-25_143201"


def test_new_id_appends_suffix_on_first_conflict() -> None:
    now = datetime(2026, 8, 25, 14, 32, 1)
    existing = {"2026-08-25_143201"}
    assert new_id(now, existing) == "2026-08-25_143201-2"


def test_new_id_finds_next_free_suffix_in_conflict_chain() -> None:
    now = datetime(2026, 8, 25, 14, 32, 1)
    existing = {"2026-08-25_143201", "2026-08-25_143201-2", "2026-08-25_143201-3"}
    assert new_id(now, existing) == "2026-08-25_143201-4"


# ---------------------------------------------------------------- id_to_datetime


def test_id_to_datetime_parses_plain_id() -> None:
    assert id_to_datetime("2026-08-25_143201") == datetime(2026, 8, 25, 14, 32, 1)


def test_id_to_datetime_parses_conflict_suffixed_id() -> None:
    """带 -2 等冲突后缀的 id 仍应正确解析出原始时间（stale 判定依赖这一点）。"""
    assert id_to_datetime("2026-08-25_143201-2") == datetime(2026, 8, 25, 14, 32, 1)


@pytest.mark.parametrize("bad_id", ["garbage", "not-a-real-id", "", "2026-13-99_999999"])
def test_id_to_datetime_returns_none_for_unparseable_id(bad_id: str) -> None:
    assert id_to_datetime(bad_id) is None
