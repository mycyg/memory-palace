# MemoryPalace 2.0 · 工作记忆

**中文** · [English](README.en.md) · [日本語](README.ja.md)

MemoryPalace 为工具和协作应用保存有来源的工作记忆。它把消息、操作结果和文档接收为来源，记录可修订的事件与知识，并在有范围和长度限制的查询中返回相关内容。Python 包和命令行名为 `eventmem`。

它适合接续跨会话的工作：保存已确认的进展、未核实的结果、待办承诺、操作经验和下次进入任务的线索。宿主负责执行任务及决定何时读取记忆。MemoryPalace 本身不运行代理，也不向用户主动发消息。

## 主要能力

- **来源与修订**：稳定的来源身份、内容快照、引用和修订历史；纠正、撤回或归档后，当前召回会重新核对有效状态。
- **工作连续性**：事件、摘要、checkpoint、commitment 和 procedure 使用同一套来源及记录流程。会话边界可保存已确认进展、未知事项与下一步入口。
- **有界召回**：按项目、集合、时间和 token 预算查找；精确匹配与 SQLite 全文索引开箱即用。可选向量和关系检索。
- **可靠处理**：SQLite 是唯一运行时真值。后台任务、索引失效与重试使用持久状态；可重建的索引不是第二份记忆库。
- **接入方式**：本地 HTTP 服务、MCP、Python／TypeScript SDK、命令行和浏览器管理台。Codex、Claude Code 与 DeepSeek Harness 集成经服务读写；离线观察先进入本地待传队列。
- **可选功能**：文档及音视频解析、embedding、关系图、显式提醒和宿主回调。基本安装不要求模型、私有配置或外部服务。

## 快速开始

需要 Python 3.10 或更新版本。仓库开发和控制台构建使用 Node.js 22 与 [uv](https://docs.astral.sh/uv/)；安装已构建的 wheel 不需要 Node.js。

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen
uv run eventmem serve --root ./example-data
```

在另一个终端写入一条合成的工作记录并召回：

```sh
uv run eventmem receive --root ./example-data --json '{"namespace":"demo","key":"rollback-1","occurred_at":"2026-09-01T00:00:00Z","scope":{"project":"demo"},"kind":"procedure","authority":"operation","text":"Rollback uses the last verified artifact."}'
uv run eventmem recall --root ./example-data --json '{"query":"rollback artifact","scope":{"project":"demo"},"scenario":"tool","budget":500}'
```

`eventmem console --root ./example-data` 可代替 `serve` 启动服务并打开管理台。服务默认监听 `127.0.0.1:8319`，数据默认放在 `~/.memorypalace`；`--root` 可为每个项目指定独立目录。命令行可直接读写本地库，HTTP 和 SDK 需要服务运行。

## 设计边界

来源的原文和模型生成的摘要分别标记。模型配置仅用于选择启用的后台能力；没有配置时，来源接收、修订和本地召回仍可使用。提醒记录计划和回调状态，收件人、渠道权限及实际发送由宿主负责。

2.0 更新了公开契约，不兼容部分 1.x 接口。升级现有数据时先备份，然后迁移到**空的独立目录**；迁移不会改写旧库。详见[安装与迁移](docs/operations.md)。

[当前架构](docs/architecture.md) · [接入与运维](docs/operations.md) · [工作示例](examples/README.md) · [工作提醒](docs/reminders.md) · [Codex 接入](docs/codex.md) · [2.0 合成基准](docs/benchmarks/work-memory-v2.md) · [历史 1.x 性能记录](docs/performance.md)

[MIT License](LICENSE)
