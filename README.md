# MemoryPalace（记忆宫殿）

> 让 AI 编程助手拥有长期记忆：记住做过的事、踩过的坑、答应过的约定。

**中文** | [English](README.en.md) | [日本語](README.ja.md)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![TypeScript](https://img.shields.io/badge/TypeScript-3178C6.svg) ![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)

MemoryPalace 给 agent 的是跨会话存活的长期记忆系统——基于事件构建 Agent 全局记忆。**事件是记忆的原子**：每段工作记录为「意图 → 行动 → 结果」的闭环；原文不可变，索引可重建，整理离线进行，注入受预算约束。

## 这是什么

AI 编程助手有个老毛病：会话一关，全忘了。

昨天调了一下午才修好的 bug，今天它原样再踩一遍。你交代过三次的规矩，第四次还得再交代。上个月试过走不通的方案，下周它又兴冲冲地再试一次。

MemoryPalace 治这个病。装上之后：

- 助手做过的每件事，自动记成一条**事件**：想干什么、干了什么、结果如何
- 下次它打开同一个文件、碰到同一个报错，相关的旧账**自动出现在它眼前**——不靠它记得去查，也不用你提醒
- 你什么都不用做：不写笔记、不打标签、不做维护。它在后台自己记、自己整理、自己淡忘不重要的

一句话：**助手会越用越懂你的项目。**

## 它怎么工作（30 秒版）

1. **记**——助手干活时，改文件、跑命令、更新任务清单，这些动作被自动记录成一条条事件。
2. **睡**——会话结束后它在后台「睡一觉」：把零碎记录归档整理，提炼教训，顺手猜一猜你明天大概要干什么。
3. **想起**——新会话开始，昨天没干完的活已经摆在桌上；打开某个文件，之前在这里栽过的跟头自动浮现。

装它之前：

> 你：把训练任务并行跑起来
> 助手：好的！（启动失败：端口冲突——上个月同一个坑，重新排查二十分钟）

装它之后：

> 你：把训练任务并行跑起来
> 助手：（记忆浮现——`[2026-07-14] 并行任务抢占同一端口，按任务 id 错开后解决`）
> 好的，端口按任务 id 错开分配——之前在这里吃过亏。

## 快速开始

### Claude Code

```bash
pip install -e .
```

把 [examples/settings.json](examples/settings.json) 里的四个 hook 合并进项目的 `.claude/settings.json`（或全局 `~/.claude/settings.json`），装完即生效：

- `SessionStart` —— 开机把「工作集」（该记得的那一屏）交给助手；首次运行自动建好 `.memory/`，零配置
- `PostToolUse` —— 干活时按线索浮现旧记忆（纯查表，毫秒级，不调用模型）
- `PreCompact` —— 上下文压缩前先把事件抢救落盘（后台）
- `SessionEnd` —— 会话结束后触发后台整理

想让整理更聪明（自动补结论、提炼教训），配一个模型即可；不配也能跑，走纯规则模式。在项目根 `.env` 或 `~/.claude/eventmem.env` 里写（参见 [.env.example](.env.example)）：

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

需要宿主机能 `import eventmem`（Python 侧负责实际写入与整理）。配置项与已知限制见 [dsh-plugin/README.md](dsh-plugin/README.md)。

**两个宿主共用同一份记忆**：昨天在 Claude Code 里踩过的坑，今天换 dsh 打开同一个文件照样浮上来。`.memory/` 跟着项目走，不跟着工具走。

## 为什么叫记忆宫殿

记忆宫殿术（method of loci）是两千年前的记忆技艺：把要记的东西一件件挂在一条熟悉路径的固定地点上，日后沿路重走，走到哪个地点，挂在那里的东西自己冒出来——不用搜，不用想，到了就想起。西塞罗用它背演讲，今天的记忆运动员用它背整副扑克牌。

本系统把「地点」换成了文件路径、报错签名、任务意图。锚点还是那个锚点，触发还是那个触发。

## 为什么这样设计：三个立场

1. **闭环事件为原子。** 不存零散知识点、不切 chunk。一个事件 = 意图 → 行动 → 结果（完成／放弃／被推翻），自包含、可整取整弃。经验（lesson）从事件里蒸馏出来，不是独立模块。
2. **联想优先，检索兜底。** 主召回路径是锚点触发的主动浮现：打开某文件、遇到某报错、开启某任务，历史结论自动出现——不依赖助手知道自己该查什么（坑的特点恰恰是踩之前不知道它在）。锚点精确匹配，无 embedding、无向量库。query 检索只服务显式考古。
3. **宁漏勿胀。** 注入受硬预算（工作集 1–2k token、浮现每次 ≤3 行、同会话去重）。漏是自愈的：漏 → 犯错 → 错被记录 → 下次它就在。胀不自愈。

## 和相似系统的区别

| 对比对象 | MemoryPalace 的区别 |
|---|---|
| query 式 RAG | 主召回是锚点触发的主动浮现（push），不是 query → embedding → top-k 检索（pull）；检索仅服务显式考古 |
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

写入与整理分离：会话中只追加，整理（闭合悬挂事件、补结论、蒸馏教训、晋升退休、重建索引与预取）全部离线——轻整理在每次会话结束后台执行，深整理按积压量触发。整理只写派生层与 lesson 字段，幂等可重跑：梦做坏了，白天的记忆还在。

设计全文见 [DESIGN.md](DESIGN.md)，实现契约见 [SPEC.md](SPEC.md)。

## CLI

```
eventmem status                  # 事件数、open 数、积压量、索引年龄、未读 CLAUDE.md 建议
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

## 出问题了怎么查

hook 的最高纪律是「永不打扰你的正常会话」：任何异常静默退出。代价是故障也无感，排查三板斧：

```bash
eventmem status                                      # 包与数据是否正常
echo '{"cwd":"'$PWD'"}' | python3 -m eventmem.hooks.session_start   # 手动跑一个 hook 看输出
tail -20 .memory/log/eventmem.log                    # 护栏日志（hook 异常都在这里）
```

## v0.2 机制一览

在闭环事件／三层架构／联想浮现之上：

- **显著性（salience）**：规则划界、自评开局、证据说了算——重要性按证据（被引用、浮现被采纳或被无视、触发推翻）离线重算，决定记忆「住哪层」而非「记不记」
- **负反馈**：浮现被无视自动降权，坏记忆自己沉底
- **预取**：整理收尾预测下次会话入口（「下次先做 X」的前瞻标记、未完成事件的关联物、模型级预测），开机那一屏自带「接下来大概要干这个」
- **分级遗忘**：hot → cold（逐出索引）→ frozen（季度纪元摘要＋整包归档，磁盘约 1/5）→ purge（仅手动）。压缩访问结构，不压缩信息——包内原文完整，`thaw` 随时解冻
- **粒度自适应**：碎事件聚组、粗事件虚拟分段，全在索引层完成
- **子 agent 归属**：委托出去的任务记为委托事件，双宿主同构
- **跨项目晋升**：可移植性判定＋敏感信息清洗双闸门后，教训可晋升为用户级
- **CLAUDE.md 建议**：反复验证的教训生成可粘贴建议，永不自动改你的文档
- **敏感信息清洗**：七类密钥模式在写入前拦截
- **评估埋点**：浮现/注入/预取全程本地 jsonl 记录，`eventmem stats` 一键出指标

## 状态

v0.2。Python 侧 341 项测试（含真实 LLM 集成与归档故障注入），dsh 插件 134 项（含跨语言互操作）；端到端演练覆盖从事件产生到冻结解冻的完整生命周期。已知限制与开放问题见 [DESIGN.md §8](DESIGN.md)。

`.memory/` 是人类可读的 markdown——玻璃盒但零维护义务：想看随时看，不看它也转。
