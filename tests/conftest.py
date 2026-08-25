"""公共测试 fixture：tmp 项目路径、事件工厂、可编程 LLM 替身。

全部测试在 tmp_path 下运行，不触碰仓库文件或真实 ~/.claude。
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from eventmem.index import GLOBAL_DIR_ENV
from eventmem.paths import MemoryPaths
from eventmem.schema import Anchors, Event, Kind, Status
from eventmem.store import Store

# 事件工厂默认 id 的起点；每次调用递增 1 秒，天然保证升序且互不冲突
_FACTORY_BASE = datetime(2026, 8, 25, 9, 0, 0)


@pytest.fixture(autouse=True)
def global_lesson_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把用户级 lesson 目录（默认 ~/.claude/eventmem，SPEC §3.18）指向 tmp。

    深整理的跨项目晋升会读写该目录。不重定向的话，本机恰好建过 ~/.claude/eventmem
    的用户跑测试就会被写进测试数据。这里只指路不创建目录——目录不存在时全部行为
    静默跳过，正是默认状态；需要该路径的测试自己 mkdir。
    """
    target = tmp_path / "global-eventmem"
    monkeypatch.setenv(GLOBAL_DIR_ENV, str(target))
    return target


@pytest.fixture
def paths(tmp_path: Path) -> MemoryPaths:
    """一个全新项目的记忆路径：<tmp_path>/project/.memory，目录已建好。"""
    p = MemoryPaths.for_project(tmp_path / "project")
    p.ensure()
    return p


@pytest.fixture
def store(paths: MemoryPaths) -> Store:
    """绑定在 `paths` 上的 L0 存储。"""
    return Store(paths)


@pytest.fixture
def event_factory() -> Callable[..., Event]:
    """事件构造工厂：只需覆盖关心的字段，其余取合理默认值。

    未显式传 id 时按调用顺序生成递增的时间戳 id（互不冲突，天然有序），
    方便需要「新近度」区分度的测试（如 surface 排序、working-set 截断）。
    """
    counter = itertools.count()

    def _make(
        *,
        id: str | None = None,
        parent: str | None = None,
        kind: Kind = "build",
        status: Status = "open",
        superseded_by: str | None = None,
        intent: str = "示例意图",
        anchors: Anchors | None = None,
        outcome: str | None = None,
        lesson: str | None = None,
        body: str = "",
        salience_prior: str | None = None,
        salience_reason: str | None = None,
        prospective: bool = False,
    ) -> Event:
        event_id = id
        if event_id is None:
            moment = _FACTORY_BASE + timedelta(seconds=next(counter))
            event_id = moment.strftime("%Y-%m-%d_%H%M%S")
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

    return _make


@dataclass
class LLMCall:
    """FakeLLMClient 记录的一次调用，供测试断言 prompt 内容与调用次数。"""

    system: str
    user: str
    max_tokens: int


class FakeLLMClient:
    """LLMClient 的替身：不发网络，不校验 base_url/model，只按队列出队响应。

    - `complete_json` 直接返回队首值（应为已解析的 Python 对象：dict/list/None）。
    - `complete` 返回队首值：字符串原样返回，其余序列化成 JSON 字符串。
    - 队首若是 BaseException 实例则 raise 而非返回，用于模拟 LLMError 等失败路径。
    - 队列耗尽仍被调用视为测试预期之外的多余调用，直接 AssertionError 暴露出来，
      而不是默默返回空值掩盖 bug。
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._queue: list[Any] = list(responses or [])
        self.calls: list[LLMCall] = []

    def queue(self, *responses: Any) -> "FakeLLMClient":
        """追加更多待出队的响应，返回 self 便于链式调用。"""
        self._queue.extend(responses)
        return self

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        value = self._pop(system, user, max_tokens)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> Any:
        return self._pop(system, user, max_tokens)

    def _pop(self, system: str, user: str, max_tokens: int) -> Any:
        self.calls.append(LLMCall(system, user, max_tokens))
        if not self._queue:
            raise AssertionError(
                f"FakeLLMClient: 队列已空但仍被调用（第 {len(self.calls)} 次调用），"
                "请检查测试里预期的 LLM 调用次数"
            )
        value = self._queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """空队列的 FakeLLMClient；测试内按需 `.queue(...)` 填充响应。"""
    return FakeLLMClient()
