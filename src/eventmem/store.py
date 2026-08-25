"""L0 存储层：一事件一文件，append-only。

不可变纪律的代码表达：本模块不提供任何修改 `intent`／`body` 的方法。允许写入的
只有生命周期字段（status/outcome/anchors/superseded_by）与整理专用的 lesson
字段（DESIGN §4.3）、闭合后一次性的 salience 自评（SPEC §3.11）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterator

from .paths import MemoryPaths, atomic_write
from .schema import (
    SALIENCE_PRIORS,
    Anchors,
    Event,
    SaliencePrior,
    SchemaError,
    Status,
    from_markdown,
    to_markdown,
)

# close 允许迁入的终态；open 不是终态
# close 只接受两个终态；superseded 一律走 mark_superseded（那里才有 by 链接）
_CLOSING_STATUSES: tuple[str, ...] = ("done", "abandoned")


class EventNotFound(KeyError):
    """按 id 找不到事件文件。"""


class AlreadyClosed(RuntimeError):
    """对已闭合事件重复执行闭合类操作。"""


class Store:
    """L0 事件存储；所有路径来自 MemoryPaths，不在本类内拼路径。"""

    def __init__(self, paths: MemoryPaths) -> None:
        """绑定记忆路径，并确保目录存在。"""
        self._paths = paths
        paths.ensure()

    @property
    def paths(self) -> MemoryPaths:
        """本存储绑定的路径对象。"""
        return self._paths

    def append(self, e: Event) -> str:
        """写入新事件；id 冲突时追加 -2、-3 后缀，返回最终 id（不修改入参对象）。"""
        if not e.id.strip():
            raise SchemaError("事件缺少 id，请先用 schema.new_id 生成")
        final_id = self._free_id(e.id)
        stored = e if final_id == e.id else replace(e, id=final_id)
        atomic_write(self._paths.event_file(final_id), to_markdown(stored))
        return final_id

    def read(self, event_id: str) -> Event:
        """读取单个事件；不存在 raise EventNotFound。"""
        path = self._paths.event_file(event_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise EventNotFound(event_id) from exc
        return from_markdown(text)

    def exists(self, event_id: str) -> bool:
        """事件文件是否存在。"""
        return self._paths.event_file(event_id).is_file()

    def all_ids(self) -> list[str]:
        """全部事件 id，升序（时间戳 id 的字典序即时间序）。"""
        events_dir = self._paths.events_dir
        if not events_dir.is_dir():
            return []
        return sorted(p.stem for p in events_dir.glob("*.md") if p.is_file())

    def iter_events(self) -> Iterator[Event]:
        """按 id 升序遍历事件；解析失败的文件跳过，不中断遍历。"""
        for event_id in self.all_ids():
            try:
                yield self.read(event_id)
            except (EventNotFound, SchemaError):
                continue

    # ---- 生命周期操作（flush 阶段，非整理）----

    def close(self, event_id: str, status: Status, outcome: str) -> None:
        """闭合事件并写入 outcome；仅 open 状态允许，重复 close raise AlreadyClosed。

        目标状态仅 done/abandoned；推翻（superseded）走 mark_superseded，
        且可发生在任何状态之后（已完成的决策也能被事后推翻）。
        """
        if status not in _CLOSING_STATUSES:
            raise ValueError(f"close 的目标状态必须是 {_CLOSING_STATUSES} 之一，收到 {status}")

        def mutate(e: Event) -> Event:
            if e.status != "open":
                raise AlreadyClosed(f"事件 {event_id} 已是 {e.status}，不可重复闭合")
            return replace(e, status=status, outcome=outcome)

        self._rewrite(event_id, mutate)

    def add_anchors(self, event_id: str, anchors: Anchors) -> None:
        """并入锚点：按类别取并集、保持既有顺序，重复调用幂等。"""

        def mutate(e: Event) -> Event:
            merged = Anchors(
                commits=_union(e.anchors.commits, anchors.commits),
                files=_union(e.anchors.files, anchors.files),
                tests=_union(e.anchors.tests, anchors.tests),
                dialog=_union(e.anchors.dialog, anchors.dialog),
                error_sigs=_union(e.anchors.error_sigs, anchors.error_sigs),
            )
            return replace(e, anchors=merged)

        self._rewrite(event_id, mutate)

    # ---- 整理专用：唯一允许整理写入 L0 的字段（DESIGN §4.3）----

    def set_lesson(self, event_id: str, lesson: str) -> None:
        """写入或重写 lesson（深整理的重蒸馏允许覆盖）。"""

        def mutate(e: Event) -> Event:
            return replace(e, lesson=lesson)

        self._rewrite(event_id, mutate)

    def set_outcome(self, event_id: str, outcome: str) -> None:
        """仅当事件已闭合且 outcome 为空时补写结论（轻整理的补全通道）。"""

        def mutate(e: Event) -> Event:
            if e.status == "open":
                raise AlreadyClosed(f"事件 {event_id} 仍为 open，outcome 应经 close 写入")
            if e.outcome not in (None, ""):
                raise AlreadyClosed(f"事件 {event_id} 已有 outcome，不可覆盖")
            return replace(e, outcome=outcome)

        self._rewrite(event_id, mutate)

    def set_salience_prior(self, event_id: str, prior: SaliencePrior, reason: str) -> None:
        """仅当事件已闭合且 prior 为空时写入自评档位与理由（整理补评通道，SPEC §3.11）。

        「当时认为它重要」是完成时事实，因此可以进 L0；但只允许写一次，
        随时间变化的后验显著性归索引层（DESIGN §8.7）。
        """
        if prior not in SALIENCE_PRIORS:
            raise ValueError(f"salience_prior 必须是 {SALIENCE_PRIORS} 之一，收到 {prior}")

        def mutate(e: Event) -> Event:
            if e.status == "open":
                raise AlreadyClosed(f"事件 {event_id} 仍为 open，闭合后才能补自评")
            if e.salience_prior is not None:
                raise AlreadyClosed(f"事件 {event_id} 已有 salience_prior，不可覆盖")
            return replace(e, salience_prior=prior, salience_reason=reason)

        self._rewrite(event_id, mutate)

    def mark_superseded(self, event_id: str, by: str) -> None:
        """标记事件被新事件推翻；已被同一事件推翻则为空操作，被他者推翻 raise AlreadyClosed。"""

        def mutate(e: Event) -> Event:
            if e.status == "superseded" and e.superseded_by not in (None, by):
                raise AlreadyClosed(f"事件 {event_id} 已被 {e.superseded_by} 推翻，不可改指 {by}")
            return replace(e, status="superseded", superseded_by=by)

        self._rewrite(event_id, mutate)

    # ---- 内部 ----

    def _rewrite(self, event_id: str, mutate: Callable[[Event], Event]) -> None:
        """读—改—原子写回；mutate 只允许返回改动了允许字段的副本。"""
        current = self.read(event_id)
        updated = mutate(current)
        if updated.intent != current.intent or updated.body != current.body:
            raise RuntimeError("不可变纪律：intent 与 body 不允许被修改")
        atomic_write(self._paths.event_file(event_id), to_markdown(updated))

    def _free_id(self, base: str) -> str:
        """从 base 起找一个未被占用的 id。"""
        if not self.exists(base):
            return base
        n = 2
        while self.exists(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"


def _union(existing: list[str], incoming: list[str]) -> list[str]:
    """有序并集：保留既有顺序，追加未出现过的新项。"""
    merged = list(existing)
    seen = set(merged)
    for item in incoming:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged
