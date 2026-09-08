# MemoryPalace 1.0 · 记忆宫殿

**中文** · [English](README.en.md) · [日本語](README.ja.md)

面向工具协作、陪伴与知识积累的单用户记忆系统。来源、事实、经历、关系、承诺、知识片段和连续性状态共享一个 Python 核心，通过插件、CLI、管理台、HTTP、MCP 与 SDK 接入。

![MemoryPalace 总架构](docs/diagrams/overview.png)

## 能力

- **可靠写入**：来源快照、内容哈希、去重、事务、修订检查、持久任务队列；模型失败保留来源和处理进度。
- **全场景记忆**：情景、事实状态、程序性经验、角色关系、共同经历、承诺提醒、日记、自述、知识与 checkpoint。项目、角色、知识库和现实／虚构领域分别设置范围。
- **统一召回**：精确线索、FTS5、LanceDB 向量、视觉向量和关系候选；融合排序、历史时点读取、纠正后的有效状态检查、上下文预算与跨宿主去重。
- **后台整理**：抽取与冲突建议、增量 Leiden 聚类、主题家族与叙事卷、引用来源的摘要／日记／画像、修订和回滚。
- **多模态**：PDF、DOCX、PPTX、XLSX、Markdown、HTML、CSV、图片、音视频；保留页码、段落、表格、时间片段与附件定位。
- **主动联系**：按角色设置提醒、承诺跟进、纪念日、检查和问候；支持时区、安静时段、频率、确认、延后、取消、投递记录和持久回调队列。
- **管理台**：处理总览、虚拟滚动浏览、来源追溯、修订对比、时间线与日历、二维／三维关系图、知识附件、日记、召回实验室、联系策略和数据维护。

模型生成内容保留生成属性。模型推断、用户明确表达和客观操作分别记录；同一来源被重复引用不会增加独立证据数量。

## 安装与启动

本次通过 GitHub 仓库交付；没有自动发布 PyPI 或 npm 包。使用 Python 3.11–3.13、Node.js 22 和 [uv](https://docs.astral.sh/uv/)：

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen --extra all --extra dev
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm ci --prefix console
npm run build --prefix console
uv run eventmem console
```

管理台和服务默认位于 `http://127.0.0.1:8319`，私有数据位于 `~/.memorypalace`。`eventmem serve` 启动服务；`--root /private/path` 指定独立目录。服务采用本地凭据、Host 和 Origin 检查。实际记忆、附件、密钥和运行日志不进入仓库。

在管理台配置抽取、冲突、摘要、重排、embedding、视觉与 ASR 等模型角色。支持兼容 API 和本地端点，允许复用模型。密钥只配置环境变量名称。未配置某类模型时，相关任务显示待配置状态。原始来源仍然保存。

## 接入与示例

| 接入 | 能力 |
|---|---|
| Claude Code 插件 | 消息和工具采集、启动恢复、操作前查询、压缩恢复、退出处理 |
| `dsh-eventmem` | DeepSeek Harness 事件适配；默认调用统一服务；保留显式旧版模式供回退 |
| HTTP `/v1` | 来源、记忆、纠正、关系、连续性、任务、维护、调度与观测 |
| MCP | stdio 与 Streamable HTTP；工具式访问 |
| Python / TypeScript SDK | 从同一 OpenAPI 生成契约；提供回调去重接口 |
| `eventmem` CLI | 服务、管理台、MCP、写入／召回、迁移、备份、调度与评估 |

MCP 工具访问本身不包含自动采集和被动注入；这些行为需要宿主事件适配。保持本地服务运行后使用插件。

```sh
uv run python examples/v1/scenarios.py tool
uv run python examples/v1/scenarios.py companion
uv run python examples/v1/scenarios.py knowledge
node examples/v1/tool.mjs
```

[示例源代码](examples/v1/)包含工具协作、共同经历／承诺、文档导入和主动联系回调。安装时未配置发送策略与渠道，只生成待发建议。回调通过稳定 delivery id 去重；不支持幂等的渠道会明确显示投递状态不确定。

## 实测

验收环境：**10 核 CPU、64 GiB 内存、SSD、macOS arm64、Python 3.13.14**。数据为 10 万条记忆、100 万个 1,024 维知识向量及对应 SQLite 记录。向量采用固定种子的 256 分量高斯混合分布，查询使用独立样本。

| 指标 | 实测 |
|---|---:|
| 本地快速召回 p95 / p99 | 20.01 / 20.74 ms |
| 百万向量查询 p95 / p99 | 39.12 / 41.29 ms |
| ANN Recall@20 | 0.9985 |
| 上下文组装（含召回）p95 | 18.76 ms |
| 后台导入期间召回 p95 | 30.32 ms |
| 峰值 RSS（含构建） | 2.75 GiB |
| 并发去重 | 10 个会话，400 次请求，200 个唯一来源 |

这些数字不包含外部模型时延，不代表真实语料上的效果保证。首次查询、常见词、后台导入、模型请求及回放结果分别报告。详见[测试条件、原始结果与复现命令](docs/performance.md)。SCARLETT 仅有图示覆盖对照，未填写推测性能，也未宣称性能优于它。

## 迁移与数据维护

```sh
uv run eventmem migrate /old/project/.memory --root /isolated/memorypalace
uv run eventmem backup /private/backup.tar.gz --root /isolated/memorypalace
uv run eventmem restore /private/backup.tar.gz --root /another/empty/root
```

迁移保留原始 id、内容、归档包、修订关联和来源指针，在独立目标校验，不覆盖旧库。缺失的外部来源明确标记。归档保留历史；永久删除处理来源及派生依赖。已导出的文件和备份需要单独管理。

## 文档与限制

- [架构与数据语义](docs/architecture.md) · [功能覆盖矩阵](docs/coverage.md) · [配置、插件、迁移与运维](docs/operations.md)
- 流程图：[写入与纠正](docs/diagrams/write-correct.svg) · [召回与上下文](docs/diagrams/recall-context.svg) · [后台整理](docs/diagrams/background.svg) · [主动联系](docs/diagrams/proactive-contact.svg)
- 所有图均包含[可编辑 Mermaid、SVG 和 PNG](docs/diagrams/)。[OpenAPI 契约](contracts/openapi.json)与 SDK、插件、管理台、媒体和文档生成进入 CI。

快速全文路径在最多 400 个近期范围内匹配中排序；深度模式支持完整匹配集排序与模型检索。PDF 默认采用原生文本解析，扫描页需要视觉端点；完整本地布局模型为可选配置。视觉检索需要兼容的多模态 embedding 端点。外部模型、重型解析和语料差异会影响端到端时延与效果。支持单用户 macOS／Linux 本地部署，不包含多用户账户、实时摄像头／麦克风采集和专用聊天平台客户端。

[MIT License](LICENSE)
