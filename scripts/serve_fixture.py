"""Serve synthetic, non-private fixtures for browser and protocol tests."""

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
    (
        "episode",
        "一次成功的数据库迁移",
        "数据库迁移在隔离目录完成。备份校验与恢复检查均通过，切换前保留旧目录入口。",
    ),
    (
        "procedure",
        "并发写入的验证方法",
        "使用稳定的来源标识去重，在事务中检查 revision。重复提交保持相同结果。",
    ),
    (
        "preference",
        "关于交流方式",
        "用户希望技术说明使用标准术语，日常聊天简短、自然。",
    ),
    (
        "relationship",
        "共同经历：秋日的花园",
        "周日一起整理花园照片。用户明确说，很喜欢那天的阳光。",
    ),
    (
        "commitment",
        "下次一起读完这篇论文",
        "下次对话继续阅读论文的实验章节，检查图表中的比较条件。",
    ),
    (
        "checkpoint",
        "项目连续性",
        "当前目标：验证记忆系统。已确认：来源可靠接收。待验证：完整规模性能。下次入口：运行评测。",
    ),
    (
        "knowledge",
        "记忆评估：时间与证据",
        "评估时仅使用当时已经出现的来源和修订。不同来源的分歧分别保留。",
    ),
    (
        "knowledge",
        "服务部署记录",
        "本地服务默认监听回环地址。宿主插件与管理台调用相同的版本化接口。",
    ),
    (
        "diary",
        "九月的一个片段",
        "今天整理了几段共同经历，也留下了下一次继续阅读的入口。",
    ),
    ("reminder", "周末阅读计划", "周末留出一段时间，继续阅读已经收藏的研究资料。"),
    ("fact", "尚待确认的项目假设", "这条模型推断等待来源复核。"),
]
ids = []
for i, (kind, title, text) in enumerate(fixture):
    source = engine.receive(
        SourceInput(
            namespace="synthetic-demo",
            key=str(i),
            kind=kind,
            title=title,
            text=text,
            authority="model" if kind in ("diary", "fact") else "explicit",
            metadata={"topic": "记忆与连续性"},
        )
    )
    ids.append(engine.source(source["id"])["record_ids"][0])
for a, b in zip(ids, ids[1:]):
    engine.relate(a, "related", b)
if not Organizer(engine).list(Scope()):
    Organizer(engine).create(Scope(), "记忆与连续性", ids, "family")
uvicorn.run(
    create_app(engine=engine, token="test-console-local", workers=False),
    host="127.0.0.1",
    port=int(os.environ.get("EVENTMEM_TEST_PORT", "8329")),
    access_log=False,
)
