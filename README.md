# MemoryPalace（记忆宫殿）

> 基于事件的 Agent 记忆系统。事件是记忆的原子：每段工作记录为「意图 → 行动 → 结果」的闭环；原文不可变，索引可重建，整理离线进行，注入受预算约束。

**中文** | [English](README.en.md) | [日本語](README.ja.md)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![TypeScript](https://img.shields.io/badge/TypeScript-3178C6.svg) ![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)

品牌名是 MemoryPalace，包名不跟着改：Python 包仍叫 `eventmem`（`pyproject.toml` 里的 `name = "eventmem"`，命令行入口同名），DeepSeek Harness 插件仍叫 `dsh-eventmem`。

MemoryPalace 给 agent 的是跨会话存活的长期记忆：不把历史压缩成会丢信息的摘要，而是把每段工作存成不可变的事件——一个闭环的情景记忆（episodic memory）单元——通过锚点触发的联想浮现取用，每次注入都卡在固定的上下文预算内。

## 命名由来

MemoryPalace 借的是记忆宫殿术（method of loci，位置记忆术）的名字：把要记的东西一件件挂在一条熟悉路径的固定地点上，日后沿着这条路径重走一遍，走到哪个地点，挂在那里的东西自动想起来，不需要主动去搜。这正是本系统「锚点触发浮现」最古典的版本——线索不是查出来的，是走到了那个地方，记忆自己冒出来的。西塞罗在《论演说家》里记过这套技艺的起源与用法；今天的记忆运动员在记忆竞技比赛里背整副扑克牌、成串数字，用的还是同一套地点编码。本系统把「地点」换成了文件路径、报错签名、todo 意图——锚点还是那个锚点，触发还是那个触发。

## 三个立场

1. **闭环事件为原子。** 不存知识点、不存 chunk。一个事件 = 意图 → 行动 → 结果（done / abandoned / superseded），自包含、可整取整弃。经验（lesson）是事件的蒸馏物，不是独立模块。
2. **联想优先，检索兜底（反 query 式 RAG）。** 主召回路径是锚点触发的主动浮现：agent 打开某文件、遇到某报错、开启某 todo，历史事件的结论单行自动出现在上下文里——不依赖 agent 知道自己该查什么。锚点精确匹配（文件路径 / 错误签名 / intent 词元），无 embedding、无向量库。query 检索只服务显式考古。
3. **宁漏勿胀。** 注入受硬预算（工作集 1–2k token、浮现每次 ≤3 行、同会话去重）。漏是自愈的：漏 → 犯错 → 错被记录 → 下次它在索引里。胀不自愈。

## 定位

三处最容易被拿来类比的系统，区别写在一行里：

| 对比对象 | MemoryPalace 的区别 |
|---|---|
| query 式 RAG | 主召回路径是锚点触发的主动浮现（push），不是 query → embedding → top-k 检索（pull）；检索仅服务显式考古 |
| MemGPT / Letta | 不做模型自主分页；注入受固定预算与工作集约束 |
| Mem0 / 事实库 | 记忆原子是「意图 → 行动 → 结果」的闭环事件，不是从对话中抽取的孤立事实点 |

## 架构

```
┌─────────────────────────────────────────────────┐
│ L2 注入层（进上下文，固定预算）                     │
│   会话启动注入工作集 ＋ 行为流触发的联想浮现          │
├─────────────────────────────────────────────────┤
│ L1 索引层（派生，可整体重建）                       │
│   working-set / 全量单行索引 / 锚点倒排 / lesson 表 │
├─────────────────────────────────────────────────┤
│ L0 存储层（append-only，不可变）                   │
│   一事件一 markdown 文件，锚点指向对话原文           │
└─────────────────────────────────────────────────┘
```

写入与整理分离：会话中只追加（flush），整理（闭合悬挂事件、补结论、蒸馏 lesson、晋升/退休、重建索引与预取）全部离线——轻整理在每次会话结束后台执行，深整理按脏量触发。整理只写派生层与 lesson 字段，幂等可重跑：梦做坏了，白天的记忆还在。

设计全文见 [DESIGN.md](DESIGN.md)，实现契约见 [SPEC.md](SPEC.md)。

## 双宿主

同一份 `.memory/` 可以被两个宿主共用：

- **Claude Code**——Python hooks（`SessionStart` / `PostToolUse` / `PreCompact` / `SessionEnd`）读写
- **DeepSeek Harness（dsh）**——原生 Cordis 插件 [`dsh-eventmem`](dsh-plugin/README.md)，B-lite 架构：TS 只走同步热路径（浮现查表、事件转写为 feed），写入 `.memory/` 与离线整理仍全部交给同一套 Python `eventmem` CLI 子进程，不产生第二套实现

`.memory/` 跟着项目走，不跟着工具走：昨天在 Claude Code 里踩过的坑，今天换到 dsh 打开同一个文件照样浮上来，反过来也一样。两个宿主共享同一份事件库、同一份锚点倒排、同一份 lesson 表，也共享同一份同会话去重集合。

## 安装

### Claude Code

```bash
pip install -e .
```

把 [examples/settings.json](examples/settings.json) 的四个 hook 合并进项目的 `.claude/settings.json`（或全局 `~/.claude/settings.json`）：

- `SessionStart` —— 注入工作集；`.memory/` 不存在时静默自举（零配置，装好即开始积累）
- `PostToolUse` —— 锚点浮现（纯查表，毫秒级，不调用 LLM）
- `PreCompact` —— 压缩前抢救式抽取（后台）
- `SessionEnd` —— 机械 flush ＋ 后台轻整理（脏量达标时深整理）

LLM 环节（事件补漏抽取、结论补写、lesson 蒸馏）为可选增强：配置 Anthropic 兼容端点即可启用，缺省时降级为纯规则模式，管道照常工作。在项目根 `.env` 或 `~/.claude/eventmem.env` 里配置（参见 [.env.example](.env.example)）：

```bash
EVENTMEM_BASE_URL=https://api.anthropic.com   # 任何 Anthropic 兼容端点均可
EVENTMEM_API_KEY=sk-...
EVENTMEM_MODEL=claude-haiku-4-5-20251001      # 小模型足够
```

已验证的兼容端点：Anthropic 官方、DeepSeek（`https://api.deepseek.com/anthropic`）。

### DeepSeek Harness（dsh）

```bash
dsh plugin --profile <name> add dsh-eventmem
```

需要宿主机能 `import eventmem`（Python 侧负责实际写入与整理）。配置项、Model Experience、已知限制见 [dsh-plugin/README.md](dsh-plugin/README.md)。

## CLI

```
eventmem status                  # 事件数、open 数、脏量、索引年龄、未读 CLAUDE.md 建议
eventmem stats [--json]          # 评估指标：浮现采纳率、注入成本、重复踩坑、预取命中、分级计数
eventmem log [--tree|--since N|--kind K]   # 事件时间线视图
eventmem search <query> [--all]  # 兜底检索（BM25）；--all 附带搜归档层（不解包）
eventmem read <id>               # 事件全文；命中冻结事件时提示 thaw
eventmem trace <id>              # 对话原文指针
eventmem consolidate --light|--deep|--deep-if-dirty
eventmem rebuild                 # 全量重建索引（L1 可随时丢弃重建）
eventmem thaw <epoch|id>         # 解冻归档事件回活跃层
eventmem purge --before <date>   # 删除更早的冻结包（默认 --dry-run，需 --yes）
eventmem init                    # 手动建 .memory/ 骨架（通常不需要，hook 会自举）
```

## 诊断

hook 以「永不打扰宿主会话」为最高纪律：任何异常静默退出、示例配置里的 `2>/dev/null || true` 连包缺失也吞掉。代价是故障也无感，排查方法：

```bash
eventmem status                                      # 包与数据是否正常
echo '{"cwd":"'$PWD'"}' | python3 -m eventmem.hooks.session_start   # 手动跑一个 hook 看输出
tail -20 .memory/log/eventmem.log                    # 护栏日志（hook 异常都在这里）
```

## v0.2 机制一览

在 v0.1 的闭环事件／三层架构／联想浮现之上：

- **显著性（salience）**：规则划界、自评开局、证据说了算——重要性是派生量，深整理时按证据（被引用、浮现被采纳/被无视、触发推翻）重算，决定「住哪层」而非「记不记」
- **负反馈**：浮现被无视自动降权，坏记忆自己沉底
- **预取**：整理收尾预测下次会话入口（前瞻标记「下次：」、open 事件关联物、模型级预测），工作集含将来时区
- **分级遗忘**：hot → cold（逐出索引）→ frozen（季度纪元摘要＋整包归档，磁盘约 1/5）→ purge（仅手动）。压缩访问结构，不压缩信息——包内原文完整，`thaw` 随时解冻
- **粒度自适应**：碎事件聚组、粗事件虚拟分段，全在索引层（L0 不可变）
- **子 agent 归属**：委托调用记为委托事件，双宿主同构
- **跨项目晋升**：可移植性判定＋敏感信息清洗双闸门后，lesson 可晋升为用户级
- **CLAUDE.md 建议**：反复验证的 lesson 生成可粘贴建议，永不自动改用户文档
- **敏感信息清洗**：七类密钥模式在写入 L0 前拦截
- **评估埋点**：浮现/注入/预取全程本地 jsonl 记录，`eventmem stats` 输出四项指标

## 状态

v0.2。Python 侧 165+ 项测试（含真实 LLM 集成与归档故障注入），dsh 插件 134 项（含跨语言互操作）；两会话端到端演练覆盖完整 hook 管道。已知限制与开放问题见 [DESIGN.md §8](DESIGN.md)。

`.memory/` 是人类可读的 markdown——玻璃盒但零维护义务：想看随时看，不看它也转。
