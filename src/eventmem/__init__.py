"""eventmem：基于事件闭环的 agent 记忆系统。

三层：L0 不可变存储（store）、L1 可重建索引（index）、召回（recall）。
本模块只导出核心公共类型与异常，具体行为见各子模块。
"""

from __future__ import annotations

from .index import Budget
from .paths import MemoryPaths
from .recall import SurfaceHit
from .schema import Anchors, Event, Kind, SchemaError, Status
from .store import AlreadyClosed, EventNotFound, Store

__version__ = "0.1.0"

__all__ = [
    "Anchors",
    "AlreadyClosed",
    "Budget",
    "Event",
    "EventNotFound",
    "Kind",
    "MemoryPaths",
    "SchemaError",
    "Status",
    "Store",
    "SurfaceHit",
    "__version__",
]
