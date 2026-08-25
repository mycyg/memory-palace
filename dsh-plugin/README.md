# dsh-eventmem

> [eventmem](../README.md) 的第二宿主适配：DeepSeek Harness (dsh) 的原生 Cordis 插件。
> 让 dsh 与 Claude Code 共用同一个 `<project>/.memory/`——在哪个宿主里做的事，另一个宿主都记得。

**In English.** `dsh-eventmem` is the DeepSeek Harness host adapter for eventmem, an
event-based memory system for coding agents. A memory is one closed loop — intent,
actions, outcome — stored as an immutable markdown file with objective anchors (file
paths, commit hashes, error signatures). The adapter does two things inside the harness
process: it injects the working set at session start, and it surfaces the one-line
outcome of past events the moment the agent touches an anchored file, hits an anchored
error, or opens a matching todo. Everything that writes to `.memory/` is delegated to
the Python `eventmem` CLI in a child process, so a single project directory can be
shared by dsh and Claude Code without two implementations drifting apart.

---

## 卖点：双宿主共享一个 `.memory/`

`.memory/` 跟着项目走，不跟着宿主走。同一个仓库：

- 用 Claude Code 时，Python hooks（`SessionStart` / `PostToolUse` / `PreCompact` / `SessionEnd`）读写它；
- 用 dsh 时，本插件读它、写 feed，并把写入与整理交回同一套 Python CLI。

两边共享同一份事件库、同一份锚点倒排、同一份 lesson 表，也共享同一份同会话去重集合
（`log/seen-<session>.txt`，格式逐字节一致）。昨天在 Claude Code 里踩过的坑，今天在 dsh 里
打开同一个文件就会浮上来。

## B-lite 架构：TS 走热路径，Python 独占写入

| 层 | 归属 | 做什么 |
|---|---|---|
| 同步读路径（浮现） | TS，进程内 | 读 `index/anchors.json` ＋ `events/*.md`，纯查表，毫秒级，不调模型、不起子进程 |
| 事件转写（feed） | TS，进程内 | 把 `tools/result` 与 `session/event` 的机械事实写成 `log/dsh-feed-<session>.jsonl` |
| 抽取与整理 | Python 子进程 | `eventmem extract` 吃 feed，`eventmem consolidate` 做轻/深整理 |
| `.memory/events/` 与 `index/` 的写入 | 只有 Python | 单一写入方，消除两实现的逐字节漂移 |

TS 侧对 `.memory/` **只读**，唯一的例外是 `log/` 下三个由它自己拥有的文件：本会话的
seen 文件、本会话的 feed 文件、适配器护栏日志 `eventmem-dsh.log`。

代价是宿主机需要一个能 `import eventmem` 的 Python 运行时。收益是 `extract` 的输入从
「解析 Claude Code transcript」变成「读结构化 JSONL」——dsh 的 `tool/result` 带 `isError`
与结构化 `value`，`todo/write` 带完整快照，都不需要防御式解析。

### 逐字节一致的三处规约

倒排索引由 Python 写、由 TS 查，两边对同一输入必须产出同一个 key。以下三处在 TS 侧是
Python 实现的逐规则复刻，并由黄金 fixture 对照测试（`tests/fixtures/*.json`，
由 `scripts/gen-fixtures.py` 直接调 Python 函数生成）：

| 规约 | Python 出处 | key 形态 |
|---|---|---|
| 错误签名 | `recall.error_signature` | `error:ValueError: port busy` |
| intent 词元化 | `index.tokenize` / `index.intent_tokens` | `intent:端口`、`intent:口冲` |
| 文件路径规约 | `paths.MemoryPaths.relative` | `file:train/launcher.py` |

Python 的 `\w` / `\s` / `\d` / `\b` 在 str 模式下是 Unicode 感知的，`splitlines()` 的换行
边界是一个字符集合，切片按码点——这些与 JS 默认行为的差异由 `src/pycompat.ts` 一次性补齐。
改动 Python 侧这三处后必须重跑 `pnpm fixtures`，否则测试会立刻暴露漂移。

## 安装

### 前置

1. Python 侧的 `eventmem` 可导入（`python3 -m eventmem.cli --help` 能跑通）。本插件对应
   **eventmem 0.1.0**：依赖 `extract` / `consolidate` / `init` / `rebuild` 四个子命令，
   `--project` 可跟在子命令参数之后，以及 `.memory/` 的目录布局（`events/`、`index/anchors.json`、
   `index/working-set.md`、`log/seen-<session>.txt`）。
2. dsh `0.1.1-rc.2` 或兼容版本（见下文「已知风险」对版本区间的说明）。

### 装入 profile

```bash
dsh plugin --profile <name> add dsh-eventmem
```

开发期也可以直接按绝对路径加载，走热重载：

```yaml
# $DSH_HOME/cordis.patch.yml
- insert:
    - id: eventmem
      name: '/绝对路径/到/dsh-plugin/src/index.ts'
      config:
        pythonExecutable: /绝对路径/到/.venv/bin/python
```

`dsh --dump-config` 可校验条目是否进入了组合。

## 配置

全部字段都有默认值，最小可用配置是空对象。

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关。关闭后一个监听器都不注册 |
| `surfaceK` | `3` | 单次浮现注入的最大行数 |
| `memoryDirName` | `.memory` | 记忆目录名。这是与 Python 版的格式契约，不是部署可变量 |
| `pythonExecutable` | `python3` | Python 可执行程序 |
| `pythonModule` | `eventmem.cli` | Python 模块名 |
| `idleDebounceSeconds` | `30` | agent 连续 idle 多少秒后触发整理 |
| `injectWorkingSet` | `true` | 会话启动是否注入工作集 |
| `writeFeed` | `true` | 是否把机械事实写成 feed |
| `runMaintenance` | `true` | idle 时是否 spawn Python 做抽取与整理 |
| `bootstrap` | `true` | `.memory/` 不存在时是否 spawn `eventmem init` 自举 |
| `toolRoles` | 见下 | 工具名 → 处理方式（`file` / `error` / `todo`） |
| `toolNameMap` | 见下 | 工具名 → Claude Code 形态工具名，供 feed 转写 |
| `delegationTools` | `["task", "subagent", "agent"]` | 委托类工具名单（小写比较）：命中的调用以委托事件形态写入 feed，供 Python 侧抽取为委托事件（SPEC §3.17） |
| `filePathKeys` | `['file_path','notebook_path','path']` | 文件类工具中依次尝试的路径入参字段名 |
| `commandKeys` | `['command','cmd']` | bash 类工具中命令入参的字段名 |

出厂的工具表覆盖 `dsh-tool-fs` / `dsh-tool-bash` / `dsh-tool-todo`：

```yaml
toolRoles:
  read: file            # dsh-tool-fs，入参 file_path
  read_image: file      # 同上
  edit: file            # 同上
  write: file           # 同上
  str_replace_editor: file   # dsh-tool-str-replace-editor，入参 path
  bash: error           # dsh-tool-bash，入参 command
  pwsh: error           # dsh-tool-pwsh，同形
  todo_write: todo      # dsh-tool-todo
toolNameMap:
  read: Read
  read_image: Read
  edit: Edit
  write: Write
  str_replace_editor: Edit
  bash: Bash
  pwsh: Bash
  todo_write: TodoWrite
```

工具名与入参字段名由部署装载的工具包决定（同一能力另有 `str_replace_editor` 等替代实现，
字段名不同），所以这两张表是配置而非硬编码。`toolRoles` 里没列出的工具名既不参与浮现也不进
feed；标为 `todo` 的工具名在 `tools/result` 上被跳过，因为 todo 快照统一由 `session/event`
的 `todo/write` 处理。

## Model Experience

### 会话启动的工作集

#### What the model sees

`index/working-set.md` 全文，作为一条 `source: { kind: 'plugin', plugin: 'eventmem', form: 'recall' }`
的用户消息进入上下文。内容形如：

```
# Memory working set (generated 2026-08-25T14:32:01)

## Open events
- [2026-08-25_143201] (build) 修复 Ray 端口冲突 — file train/launcher.py

## Recent outcomes
- [2026-08-24_110300] 为每个任务分配独立端口区间，冲突消除

## Lessons (promoted)
- 多任务共用 Ray 时先分配端口区间 [2026-08-24_110300]
```

#### Token effect

工作集由 Python 侧按硬预算生成，默认上限 1500 token（估算口径：字符数 // 3）。注入量不随
记忆总量增长——装不下的部分留在 `index/project.md` 等待检索。

#### KV Cache effect

注入发生在 `agent/session-start`，落在 agent inbox 的 `next-step` 边界，位于会话最前部，
之后不再变动，因此不会使后续请求的前缀失效。compact 之后 dsh 会以 `source: 'compact'`
重新触发一次会话启动，工作集随之重新供给。

### 锚点浮现

#### What the model sees

一条不超过 `surfaceK` 行的消息，同样是 `form: 'recall'`：

```
Memory:
[2026-08-24_110300] 为每个任务分配独立端口区间，冲突消除
```

触发时机是三类线索之一命中倒排索引：打开／改写某个有历史事件的文件、bash 以非零退出码收尾
且错误签名命中过往 `fix` 事件、把某条 todo 标为 `in_progress` 且其 intent 词元命中历史事件。

#### Token effect

每次触发 ≤ `surfaceK` 行，每行 ≤120 字符（按码点截断）。同一事件在同一会话内只浮现一次，
去重集合落在 `log/seen-<session>.txt`。

#### KV Cache effect

浮现是增量追加的用户消息，插在工具结果之后，不改写既有上下文，因此只影响其后的前缀。
高频触发会增加 turn 内的消息数量——`surfaceK` 与 seen 去重共同为此设上限。

## Known Limitations and Deferred Work

| 编号 | 限制 | 说明 |
|---|---|---|
| L-1 | 需要 Python 运行时 | B-lite 的代价。`pythonExecutable` 不可用时浮现与工作集注入照常工作，只是不再产生新事件；失败只记日志 |
| L-2 | `agent.inject` 不保证送达 | dsh 的语义即如此：「It may miss a request whose pre-step already claimed its batch.」改用 `agent.steer` 可保证唤醒，代价是可能多起一个 turn；本插件选择不打扰 driver |
| L-3 | compact 前没有抢救时机 | dsh 中不存在与 `PreCompact` 等价的可订阅扩展点。所幸 dsh 的会话日志 append-only，compact 只给区间打影子标记，历史不丢，因此本插件改为流式落盘，不依赖抢救 flush |
| L-4 | feed 只记机械事实 | 不写对话原文，因此 Python 侧 `extract` 的 LLM 判断层拿不到对话摘录，只有机械收集层生效。声明式事件（todo 开闭）与四类锚点不受影响 |
| L-5 | 未订阅 `tools/post-execute` | 浮现走 emit 模式的 `tools/result`，不参与 waterfall，因此不会改写工具结果也不会阻断后续监听器。代价是注入落在下一个 step 边界而非紧跟该工具结果 |
| L-6 | 单进程多项目 | 状态按 `session.id` 隔离，`.memory/` 位置取 `session.header.cwd`，多项目可并存；但 `pythonExecutable` 是进程级配置，全部项目共用 |
| L-7 | `.memory/` 存在性缓存 5 秒 | 自举完成后最多 5 秒才转为可用，期间的工具结果不浮现也不进 feed |
| L-8 | 路径规约的 `..` 折叠顺序 | Python 的 `Path.resolve()` 逐段解析符号链接，TS 侧先做词法折叠再解析。含 `..` 且穿过符号链接的路径可能得到不同结果；工具给出的 `file_path` 通常已是干净的绝对路径，未观察到实际分歧 |
| L-9 | peer 版本区间与 dsh 的预发布节奏不合 | dsh 只发 `-rc` 预发布版。npm 的 semver 规则下 `^0.1.1-rc.2` 匹配 `0.1.1-rc.2` / `0.1.1` / `0.1.2`，但**不匹配** `0.1.2-rc.1`——下一个 rc 一出，本包的 peer 区间就会告警。同时 dsh 各包的 `latest` dist-tag 仍指向很旧的版本（`dsh-tools` 的 latest 是 `0.0.1-rc.1`），`0.1.1-rc.2` 在 `next` 标签下；安装时必须写明版本或用 `@next` |

## 开发

```bash
npm install
npm run typecheck     # tsc --noEmit
npm test              # vitest run
npm run fixtures      # 重新生成黄金 fixture（需要 ../.venv）
npm run build         # 产出 lib/
```

测试分五层：黄金 fixture 对照（签名／词元／文件 key）、浮现排序与预算、运行时与工具名映射、
插件装配与护栏纪律、以及与 Python 的互操作集成测试——后者直接 spawn `../.venv/bin/python -m
eventmem.cli extract` 吃本插件生成的 feed，断言事件真的落进了 `.memory/events/`。没有可用的
`.venv` 时该组测试自动跳过。

## 许可

MIT
