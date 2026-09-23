"""Serve a synthetic work-memory fixture for browser tests."""

import os
from pathlib import Path

import uvicorn

from eventmem.core import Engine, SourceInput
from eventmem.core.api import create_app
from eventmem.core.models import Scope
from eventmem.core.organize import Organizer

root = Path(os.environ.get("EVENTMEM_TEST_ROOT", ".work/console-fixture"))
engine = Engine(root)
fixture = [
    ("episode", "隔离目录迁移", "数据库迁移在隔离目录完成。备份校验与恢复检查均通过。"),
    ("procedure", "并发写入验证", "使用稳定来源标识去重，在事务中检查 revision。"),
    ("commitment", "发布前复核", "发布前复核索引新鲜度和服务健康回执。"),
    ("checkpoint", "项目续接", "已确认：来源接收。待验证：规模性能。下次入口：运行评测。"),
    ("knowledge", "评估证据边界", "评估只使用当时可见的来源与修订。"),
    ("reminder", "复核时间", "到期后检查项目发布条件。"),
]
ids = []
for index, (kind, title, content) in enumerate(fixture):
    source = engine.receive(
        SourceInput(
            namespace="synthetic-work",
            key=str(index),
            kind=kind,
            title=title,
            text=content,
            authority="explicit",
        )
    )
    ids.append(engine.source(source["id"])["record_ids"][0])
if not Organizer(engine).list(Scope()):
    Organizer(engine).create(Scope(), "迁移与验证", ids[:3], "event")

uvicorn.run(
    create_app(engine=engine, token="test-console-local", workers=False),
    host="127.0.0.1",
    port=int(os.environ.get("EVENTMEM_TEST_PORT", "8329")),
    access_log=False,
)
