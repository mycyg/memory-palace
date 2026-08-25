# eventmem 第二宿主适配规格：DeepSeek Harness (dsh)

调研对象：`deepseek-harness` @ `0.1.1-rc.2`，git `b150a55`（仓库根 `package.json:3`）。

本文所有 dsh 源码路径相对于 dsh 仓库根。所有 API 断言都给出源码文件与行号；查不到的一律写「未找到」，不做推测。eventmem 侧路径相对于本仓库 `event-memory/`。

---

## 0. 两条路线的结论

| 路线 | 结论 | 一句话 |
|---|---|---|
| A：`@deepseek-ai/dsh-hooks-claude-code` 兼容层 | 不改 Python 代码约 15%–20% 可用；只改 `settings.json` 仍约 20%；额外加一层配置层的工具名翻译 shim 可到约 45% | 桥接只实现 Claude Code 30 个钩子点中的 7 个，`PreCompact` 与 `SessionEnd` 都不在其中；且 dsh 的工具名与 transcript 格式与 Claude Code 均不同 |
| B：原生 Cordis 插件 | 除「compact 前抢救 flush」外全部时机都有对应扩展点；`.memory/` 逐字节兼容可达成 | 写入侧（`tool/result`／`todo/write`）拿到的数据比 Claude Code transcript 更结构化；抽取阶段不需要解析 transcript |

---

# 第一节 路线 A：hooks-claude-code 兼容层

## 1.1 桥接支持的钩子点

`packages/hooks/hooks-claude-code/src/config.ts:11-19` 是唯一的白名单，逐字：

```ts
const CLAUDE_EVENTS = [
  'SessionStart',
  'UserPromptSubmit',
  'PreToolUse',
  'PostToolUse',
  'Stop',
  'SubagentStart',
  'SubagentStop',
] as const
```

`parseClaudeCodeConfig` 只遍历这个数组（`config.ts:86`），因此配置文件里 `PreCompact` 与 `SessionEnd` 两段在解析阶段就被跳过，不产生任何警告，也不影响其余钩子注册。

映射到 dsh 内部事件（`packages/hooks/hooks-claude-code/README.md:37-46`，与 `src/index.ts` 的 `ctx.on` 调用逐条对应）：

| Claude Code 钩子 | dsh 扩展点 | 分发模式 | 注册位置 | eventmem 是否需要 |
|---|---|---|---|---|
| `SessionStart` | `agent/session-start` | emit（detached） | `src/index.ts:206` | 需要 |
| `UserPromptSubmit` | `agent/pre-step` | waterfall | `src/index.ts:219` | 不需要 |
| `PreToolUse` | `tools/pre-execute` | waterfall | `src/index.ts:238` | 不需要 |
| `PostToolUse` | `tools/post-execute` | waterfall | `src/index.ts:247` | 需要 |
| `Stop` | `agent/turn-stopping` | serial | `src/index.ts:270` | 不需要（可作 `SessionEnd` 的替代触发点，见 1.6） |
| `SubagentStart` | `subagent/start` | emit（detached） | `src/index.ts:281` | 不需要 |
| `SubagentStop` | `subagent/end` | emit（detached） | `src/index.ts:291` | 不需要 |
| `PreCompact` | 无 | — | — | **需要，未支持** |
| `SessionEnd` | 无 | — | — | **需要，未支持** |

`PreCompact`／`PostCompact`／`SessionEnd` 在 `packages/hooks/hooks-claude-code/README.md:89` 的「Unsupported hook events (23 of Claude Code's current 30)」清单中被逐一点名。compact 的两个钩子点是显式的范围外决策，`.agents/notes/implemented/feature/2026-06-30-interception-extension-points.md:50`：

> Compaction (`PreCompact`/`PostCompact`), Notification, and Codex `PermissionRequest` remain outside this decision.

## 1.2 stdin JSON 字段兼容度

### 1.2.1 基础字段

`packages/hooks/hooks-claude-code/src/index.ts:322-331`，逐字：

```ts
function base(ctx: Context, agent: Agent | undefined, event: string): Record<string, unknown> {
  return {
    session_id: agent?.session.header.id ?? '',
    transcript_path: agent === undefined
      ? ''
      : ctx.get('sessionPersistence')?.locate(agent.session.header)?.path ?? '',
    cwd: agent?.session.header.cwd ?? process.cwd(),
    hook_event_name: event,
  }
}
```

| 字段 | 提供 | 说明 |
|---|---|---|
| `cwd` | 是 | `agent.session.header.cwd`，即 `session/new` 时的会话工作目录 |
| `session_id` | 是 | `agent.session.header.id` |
| `transcript_path` | 是（可能为空串） | 见 1.2.3 |
| `hook_event_name` | 是 | eventmem 不读取 |

`README.md:96` 写「mapped event payloads omit `prompt_id`, `transcript_path`, …」，与 `src/index.ts:322-331` 的实现相矛盾；实现为准，`transcript_path` 存在。该矛盾原因未查明。

### 1.2.2 PostToolUse 专有字段

`packages/hooks/hooks-claude-code/src/index.ts:342-344`：

```ts
function postToolPayload(ctx: Context, exec: ToolExecution, result: ToolExecutionResult): Record<string, unknown> {
  return { ...base(ctx, exec.agent, 'PostToolUse'), tool_name: exec.name, tool_input: exec.arguments, tool_use_id: exec.callId, tool_response: blocksToText(result.content) }
}
```

| 字段 | 提供 | 与 Claude Code 的差异 |
|---|---|---|
| `tool_name` | 是 | 取值为 dsh 的工具名，与 Claude Code 不同（见 1.5 表 A-2） |
| `tool_input` | 是 | `exec.arguments`，类型为 `unknown`（`packages/core/tools/src/index.ts:323`），即模型产出的已解析入参 |
| `tool_use_id` | 是 | eventmem 不读取 |
| `tool_response` | 是，但为**字符串** | Claude Code 对 Bash 提供 `{"stdout","stderr","interrupted","isImage"}` 对象；桥接用 `blocksToText` 拍平为纯文本（`src/index.ts:318-320`） |

`blocksToText` 只保留 `type === 'text'` 的块并拼接（`src/index.ts:318-320`）。

### 1.2.3 transcript_path 指向什么

`transcript_path` 由 `ctx.get('sessionPersistence')?.locate(header)?.path` 解析：

- JSONL 后端返回真实路径：`packages/session/session-persistence-jsonl/src/index.ts:172-174`，`locate` 返回 `{ kind: 'jsonl', path: logPath(this.root, meta.cwd, meta.id, this.compression) }`。
- SQLite 后端返回 `undefined`：`packages/session/session-persistence-sqlite/src/index.ts:93`，`locate(_meta: SessionHeader): SessionLocation | undefined`。此时 `transcript_path` 为空串。
- 抽象基类：`packages/session/session-persistence/src/index.ts:96`。

即使拿到 JSONL 后端的路径，与 eventmem 的 `extract.py` 仍存在三层不兼容：

| 层级 | dsh 的形态 | extract.py 的假设 | 影响 |
|---|---|---|---|
| 物理编码 | 默认 `.jsonl.zstd`，为若干独立 Zstandard 帧的拼接（`packages/session/session-persistence-jsonl/README.md:5,36`）；仅 `compression: 'none'` 时是原始 `.jsonl` | 以 UTF-8 逐行文本打开（`src/eventmem/extract.py:358`） | 默认配置下每一行都不是合法 JSON，`json.loads` 全部失败，计入 `skipped_lines`，抽取结果为空 |
| 首行 | 不可变的 `SessionHeader`，标记 `{ type: 'session', version, id, cwd?, createdAt, … }`（`packages/session/session-persistence-jsonl/README.md:17`） | 无对应处理 | `_scan_record` 的 `kind` 取到 `'session'`，`role` 落到 `str(kind)`，不进入任何分支 |
| 每行记录 | 一条 `SessionEvent`，形如 `{ type, seq, time, data }`（`packages/core/session/src/types.ts:408-415`）；`type` 取值为 `'user/message'`／`'assistant/message'`／`'tool/call'`／`'tool/result'`／`'todo/write'` 等（`packages/core/session/src/types.ts:236-336`） | `record.get("type") or record.get("role")`，期望 `'user'`／`'assistant'`；`record["message"]["content"]` 为块数组；顶层 `record["toolUseResult"]` 为 Bash 结构化结果（`src/eventmem/extract.py:394-408`） | 三处取值全部落空，`Harvest` 为空，`extract_events` 返回 0 个新事件 |

另有一项差异：JSONL 后端在 `packChunks` 为默认 `true` 时会把连续的 `assistant/chunk` 增量事件打包成单行「packed chunk row」（`packages/session/session-persistence-jsonl/README.md:18`），一行承载多条事件。任何按「一行一记录」假设的解析都会再偏离一层。

**结论：`transcript_path` 字段存在，但指向的文件对 eventmem 现有解析器等价于不可用。**

## 1.3 输出语义

`hookSpecificOutput.additionalContext` 在 `SessionStart` 与 `PostToolUse` 下都被消费，前提是 `hookEventName` 与正在触发的事件名严格相等。

解码规则，`packages/hooks/hook-protocol/src/codec.ts:115-133`：

```ts
  const hso = obj(parsed.hookSpecificOutput)
  if (hso) {
    const eventName = str(hso, 'hookEventName')
    if (eventName !== undefined) output.hookEventName = eventName
    // A missing or mismatched discriminator cannot affect the firing event.
    if (expectedEventName !== undefined && eventName !== expectedEventName) {
      return
    }
    …
    const addCtx = str(hso, 'additionalContext')
    if (addCtx !== undefined) output.additionalContext = addCtx
```

结构化 stdout 只在退出码为 0 且 stdout 去空白后以 `{` 起始时才尝试解析（`codec.ts:72-85`）。eventmem 的 `run_hook` 恒以 `sys.exit(0)` 结束、输出 `json.dumps(...)`（`src/eventmem/hooks/__init__.py:85-90`），且 `session_start.py:43-46`、`post_tool_use.py:73-76` 写的 `hookEventName` 分别为 `"SessionStart"` 与 `"PostToolUse"`，与桥接传入的 `expectedEventName: point`（`src/index.ts:172`）一致。**该输出格式被逐字接受。**

注入位置：

| 钩子点 | 注入方式 | 源码 | 模型看到的位置 |
|---|---|---|---|
| `SessionStart` | `agent.inject(context)` | `src/index.ts:210` | 进入 agent inbox 的 `next-step` 边界，不唤醒 driver（`packages/core/agent/src/runtime-types.ts:135-143`） |
| `PostToolUse` | `PostToolDecision.additionalContexts` | `src/index.ts:252,259-264` | 作为独立的 `user/message` 附在该工具结果之后 |

两者都被打上 `{ kind: 'plugin', plugin: 'hooks-claude-code' }` 来源（`src/index.ts:87`），在会话日志中记为 `user/message` 而非用户输入。

`SessionStart` 的时序不保证。`src/index.ts:204-205` 有未完成项：

```ts
  // TODO(session-start-gating): add a startup gate before promising first-turn delivery.
```

叠加 `inject` 自身的语义（`runtime-types.ts:135-140`：「It may miss a request whose pre-step already claimed its batch」），工作集有可能错过第一次模型请求。

## 1.4 启用步骤草案

dsh 的配置为 YAML 数组，每行一个 `{ id, name, config }` 条目。层叠顺序（`apps/cli/reference/README.md:9`）：各 bundle 的 `cordis.patch.yml` → `$DSH_HOME/profiles/<name>/cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → 命令行 `--patch <path>`。`$DSH_HOME` 默认 `~/.dsh`（`packages/util/home-paths/src/index.ts:12,61-63`）。没有项目目录内自动发现的配置文件。

插件 Config 声明，`packages/hooks/hooks-claude-code/src/index.ts:45-78`：

```ts
export interface Config {
  configPath: string
  pluginRoot?: string
  projectDir?: string
  defaultTimeoutMs?: number
  stderrSummaryMaxChars?: number
}
```

`configPath` 为必填，可以是 `hooks.json`，也可以是带 `hooks` 键的 settings 文件（`config.ts:82-83` 两种都接受）——eventmem 的 `examples/settings.json` 属于后者，可直接指向。

步骤：

1. 确认组合中已加载 `ctx.shell` 的提供者（`export const inject = ['shell']`，`src/index.ts:42`）。`packages/bundle/base` 已包含 `@deepseek-ai/dsh-bash-local`。
2. 在 `$DSH_HOME/cordis.patch.yml` 追加：

```yaml
- insert:
    - id: eventmem-hooks
      name: '@deepseek-ai/dsh-hooks-claude-code'
      config:
        configPath: /绝对路径/到/event-memory/examples/settings.json
        projectDir: /绝对路径/到/被管理项目
```

3. `dsh --dump-config` 校验条目已进入组合（`docs/user/develop/basic/publish.md:106`）。
4. 保证 `python3 -m eventmem.hooks.*` 在 dsh 进程的 PATH 与 `PYTHONPATH` 下可导入。钩子进程的工作目录被设为会话工作目录（`src/index.ts:147,166`），`CLAUDE_PROJECT_DIR` 环境变量被导出（`src/index.ts:150-151`）。

限制：`configPath` 是**进程级**的，加载时读取一次，相对路径按进程启动目录解析，没有按会话发现项目内 `hooks.json` 的能力（`src/index.ts:48-52`，`TODO(per-session-hook-config)`）。单进程服务多个项目时，全部项目共用同一份钩子配置。

## 1.5 兼容度结论与缺口清单

### 表 A-1：逐钩子判定

| eventmem 钩子 | 桥接支持 | 零改动可用度 | 阻塞原因 |
|---|---|---|---|
| `SessionStart`（注入工作集） | 是 | 约 85% | 功能通路完整；注入时机不保证（`TODO(session-start-gating)`），可能错过第一次请求 |
| `PostToolUse`（浮现） | 是 | 0% | matcher 字符串不命中任何 dsh 工具名 |
| `PreCompact`（抢救 flush） | 否 | 0% | 事件不在白名单，配置被静默忽略 |
| `SessionEnd`（机械 flush ＋ 后台整理） | 否 | 0% | 同上 |

### 表 A-2：工具名对照

`matchesMatcher` 在 `claude-code` 模式下，对纯 `[A-Za-z0-9_|]+` 的模式按**字面量精确匹配**处理，管道符为精确选项分隔（`packages/hooks/hook-protocol/src/matcher.ts:18,61-63`）：

```ts
  if (mode === 'claude-code' && CLAUDE_LITERAL.test(pattern)) {
    return pattern.split('|').includes(query)
  }
```

eventmem 的 matcher 为 `"TodoWrite|Read|Edit|Write|MultiEdit|NotebookEdit|Bash"`（`examples/settings.json:17`），属于该模式，因此大小写敏感、逐字比较。

| eventmem matcher 选项 | dsh 工具名 | 提供包 | 源码 |
|---|---|---|---|
| `Read` | `read` | `@deepseek-ai/dsh-tool-fs` | `docs/tool-catalog.md:27` |
| `Edit` | `edit` | 同上 | 同上 |
| `Write` | `write` | 同上 | 同上 |
| `MultiEdit` | 未找到对应工具 | — | — |
| `NotebookEdit` | 未找到对应工具 | — | — |
| `Bash` | `bash` | `@deepseek-ai/dsh-tool-bash` | `docs/tool-catalog.md:21` |
| `TodoWrite` | `todo_write` | `@deepseek-ai/dsh-tool-todo` | `docs/tool-catalog.md:41` |

七个选项零命中。

### 表 A-3：入参 schema 对照

| 工具 | dsh 入参字段 | eventmem 读取字段 | 结果 |
|---|---|---|---|
| `read`／`edit`／`write` | `file_path`（必填）（`docs/tool-catalog.md:630-746`） | `file_path`／`notebook_path`／`path` 依次尝试（`src/eventmem/hooks/post_tool_use.py:134`） | 字段名一致，可用 |
| `todo_write` | `todos: [{ content, status }]`，`status ∈ {pending, in_progress, completed}`（`packages/core/session/src/types.ts:179-194`；`docs/tool-catalog.md:41`） | `todos[].status`／`todos[].content`／`todos[].activeForm`（`post_tool_use.py:93-104`） | 结构同构；`activeForm` 在 dsh 不存在，eventmem 已有 `content` 优先的回退，可用 |
| `bash` | 结构化结果在 `ToolExecutionSuccess.value`，形如 `{ kind, exitCode, signal, timedOut, aborted, timeoutMs, stdout: {text, truncated, spillPath?}, stderr: {…}, sandbox? }`（`packages/shell/tool-bash/src/index.ts:158-181`，输出 schema 在 `:275-320`） | `tool_response` 需为 `dict`，读 `stdout`／`stderr`／`interrupted`（`post_tool_use.py:168-178`） | **不可用**：桥接把 `result.content` 拍平为字符串，`isinstance(tool_response, dict)` 为假，`_bash_error_text` 恒返回空串，错误签名浮现完全失效 |

### 缺口清单

| 编号 | 缺口 | 影响的 eventmem 能力 | 是否可在配置层绕过 |
|---|---|---|---|
| A-1 | 桥接不支持 `PreCompact` | compact 前的抢救式抽取全部丢失 | 否 |
| A-2 | 桥接不支持 `SessionEnd` | 会话结束的机械 flush、轻整理、深整理调度全部不触发 | 部分（见 1.6 的 `Stop` 替代方案） |
| A-3 | 工具名不匹配 | 三条浮现通路全部零命中 | 是（改 matcher 字符串） |
| A-4 | `post_tool_use.py` 内部按 Claude Code 工具名分派 | 同上；即使 matcher 改对，分派仍落空 | 是（配置层加名称翻译 shim） |
| A-5 | `tool_response` 被拍平为字符串 | Bash 错误签名浮现失效 | 否 |
| A-6 | transcript 为 zstd 压缩且 schema 不同 | `extract_events` 抽取不到任何事件 | 否 |
| A-7 | `SessionStart` 为 detached 注入，时机不保证 | 工作集可能错过第一次请求 | 否 |
| A-8 | `configPath` 进程级、加载时读一次 | 多项目场景下钩子配置无法按项目区分 | 否 |
| A-9 | 匹配的多个 handler 串行执行且不去重（`README.md:97`） | 对 eventmem 无影响（每个事件仅一个 handler） | — |

### 综合可用度

| 场景 | 可用度 | 说明 |
|---|---|---|
| 完全不改动（现有 `settings.json` ＋ 现有 Python） | 15%–20% | 仅 `SessionStart` 的工作集注入生效，且时机不保证。四条数据流中，写入侧 0%、浮现侧 0%、注入侧 100% |
| 只改 `settings.json` 的 matcher 字符串 | 约 20% | matcher 命中后 payload 进入 `post_tool_use.py`，但 `_FILE_TOOLS`／`== "TodoWrite"`／`== "Bash"` 三处分派仍全部落空（`post_tool_use.py:31,59,63`） |
| 加配置层工具名翻译 shim（不动 eventmem 源码） | 约 45% | `SessionStart` 通、文件锚点浮现通、todo 意图浮现通；Bash 错误浮现仍为 0（A-5），`PreCompact`／`SessionEnd` 仍为 0（A-1／A-2） |

## 1.6 缺口的 workaround

### A-3 ＋ A-4：工具名翻译

`settings.json` 的 `command` 字段是 shell 命令行，通过 `ctx.shell` 执行（`packages/hooks/hook-protocol/README.md:23`），因此可在管道里插入一段翻译，不需要改动 eventmem 源码：

```json
{
  "matcher": "todo_write|read|edit|write|bash",
  "hooks": [
    {
      "type": "command",
      "command": "jq -c '.tool_name |= ({\"todo_write\":\"TodoWrite\",\"read\":\"Read\",\"edit\":\"Edit\",\"write\":\"Write\",\"bash\":\"Bash\"}[.] // .)' | python3 -m eventmem.hooks.post_tool_use 2>/dev/null || true",
      "timeout": 10
    }
  ]
}
```

该方案引入 `jq` 依赖。等价的纯 Python 前置进程也可行。翻译后 A-5 仍然存在：`tool_response` 是字符串，Bash 分支无输出。

### A-2：用 `Stop` 近似 `SessionEnd`

`Stop` 映射到 `agent/turn-stopping`（serial，awaited，`src/index.ts:270`），每个 turn 结束前触发一次，而非每个会话一次。可作为增量 flush 的触发点，代价是：

- 触发频率远高于 `SessionEnd`，每 turn 一次；
- eventmem 的 `session_end.py` 会在每次触发时同步跑一次机械抽取并拉起后台 consolidate，需要自行加节流；
- `agent/turn-stopping` 被 await（`packages/core/agent-loop/src/agent.ts:295`），同步部分会延后 turn 关闭；
- 桥接对 `Stop` 未实现连续阻塞上限（`src/index.ts:269`，`TODO(stop-loop-guard)`），但 eventmem 不返回阻塞决策，不受影响。

由于 A-6（transcript 不可解析），即使触发成功，同步的 `extract_events` 也抽不到事件，只有后台 consolidate 会跑。因此这条 workaround 的实际收益仅限于「让轻／深整理有机会被调度」。

### A-1：无 workaround

compact 前没有任何可订阅的时机（见 2.2）。

---

# 第二节 路线 B：原生 TS 插件规格

## 2.1 插件标准形态

### 2.1.1 三种插件形态与硬规则

`packages/AGENTS.md:5`：

> service packages default-export their service class; function plugins named-export `name` / `inject` / `Config` / `apply` and have no default export. Mixing the forms makes the Loader discard the function plugin's namespace

eventmem 适配器应采用**函数插件**形态（不注册服务，只订阅事件）。误加 `export default` 会让 Loader 的 `unwrapExports` 折叠模块并丢弃 `inject`（`packages/todo/tool-todo/README.md:37`；事故记录 `docs/postmortem/0001-acp-default-export-drops-inject.md`）。

### 2.1.2 入口文件导出符号

以 `packages/todo/tool-todo/src/index.ts` 为参照：

| 符号 | 行 | 形态 |
|---|---|---|
| `import type { Context } from '@deepseek-ai/cordis'` | `:8` | 类型导入 |
| `import z from '@deepseek-ai/schemastery'` | `:9` | 配置 schema 库，既作类型又作值 |
| `export const name = 'tool-todo'` | `:22` | 插件名 |
| `export const inject = ['tools']` | `:23` | 依赖的服务键；可省略 |
| `export interface Config { … }` | `:29` | 配置类型 |
| `export const Config: z<Config> = z.object({ … })` | `:41` | 配置 schema |
| `export function apply(ctx: Context, config: Config): void` | `:128` | 入口 |

`Config` 必须是 Standard Schema 校验器，不能导出普通对象（`docs/user/develop/basic/config.md:45`）。无配置时 `apply` 只取 `ctx` 一个参数（`packages/schedule/schedule/src/index.ts:40`）。

`inject` 中可用的服务键（本适配器需要）：`agents`、`sessions`、`llm`、`tools`。可选服务用 `ctx.get(name)` 读取，不用 `ctx.<name>` 属性（`packages/AGENTS.md:6`）。

### 2.1.3 package.json 约定

第三方插件不受 `scripts/check-workspace-constraints.ts` 约束（该脚本只扫描 workspace 成员）。官方包名模式为 `@deepseek-ai/dsh-<name>`（`AGENTS.md:101`）；第三方文档示例使用**无 scope 的 `dsh-` 前缀**，`docs/user/develop/basic/publish.md:37` 的 `"name": "dsh-hello-plugin"`。**`dsh-plugin-*` 包名约定：未找到。**

关键字段（`docs/user/develop/basic/publish.md:36-43`）：

```json
{
  "name": "dsh-eventmem",
  "version": "0.1.0",
  "type": "module",
  "main": "lib/index.js",
  "types": "lib/types/index.d.ts",
  "files": ["lib", "cordis.patch.yml"],
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
}
```

`dsh.bundle.patch` 是 bundle 自动激活的依据，缺失会在 profile 加载时抛错（`packages/boot/app-boot/src/profile.ts:391-393`）。

peerDependencies 参照 `packages/todo/tool-todo/package.json:43-50`：`@deepseek-ai/cordis`（vendor 版本 4.0.1，`vendor/cordis/package.json:4`）、`@deepseek-ai/dsh-agent`、`@deepseek-ai/dsh-session`、`@deepseek-ai/dsh-tools`、`@deepseek-ai/dsh-llm`。dsh 包当前版本 `0.1.1-rc.2`。仓库内部一律用 `workspace:^`，**第三方可用的稳定版本区间：未找到**，需按 npm 实际版本填写。

### 2.1.4 加载与安装

第三方插件按裸包名解析，锚点是 profile 目录的 Node 父级查找（`apps/cli/reference/README.md:11`）。安装通过 `dsh plugin --profile <name> add <spec>`，等价于以 profile 目录为工作目录调用 pnpm（`apps/cli/reference/README.md:43`）。bundle 成员变更需要重启，profile 或 home 的 `cordis.patch.yml` 普通编辑走热重载（`apps/cli/reference/README.md:55`）。

开发期可用绝对路径直接加载（`docs/user/develop/basic/index.md:50-56`）：

```yaml
- insert:
    - id: eventmem
      name: '/绝对路径/到/dsh-eventmem/src/index.ts'
```

### 2.1.5 README 约定

`packages/AGENTS.md:26-27` 要求包 README 含 `## Model Experience`（下辖 `### <条目>` ＋ `#### What the model sees`／`#### Token effect`／`#### KV Cache effect`）与 `## Known Limitations and Deferred Work` 两节。校验脚本 `scripts/verify-package-readme-limitations.ts:36` 只扫描 `packages/*/*/package.json`，**第三方插件不受该门禁约束**，但结构值得沿用。

### 2.1.6 发布约定

| 项目 | 结论 | 源码 |
|---|---|---|
| 官方 npm scope | `@deepseek-ai/dsh-<name>` | `AGENTS.md:101` |
| 第三方包名约定 | 无强制；文档示例用无 scope 的 `dsh-` 前缀 | `docs/user/develop/basic/publish.md:37` |
| npm keywords 约定 | 未找到（仓库内零个 `"keywords"` 字段） | — |
| 发现渠道 | GitHub 仓库 topic `dsh-plugin` | `README.md:42`；`CONTRIBUTING.md:15` |
| 官方插件列表／注册表／市场 | 未找到 | — |
| 分发方式 | git host（需 `prepare` 脚本 ＋ 用户在 `pnpm-workspace.yaml` 的 `allowBuilds` 显式放行）／npm／`pnpm pack` 产出的 tarball | `docs/user/develop/basic/publish.md:160-178` |

## 2.2 事件映射表

分发模式语义（`docs/cordis-api/events.md:193-204`）：`emit` 不等待监听器；`parallel` 并发等待全部；`serial` 按序等待直到有人 bail；`waterfall` 以 `next` 回调层层包裹。

| eventmem 需求 | dsh 事件名 | 模式 | 监听器签名 | 源码 |
|---|---|---|---|---|
| 会话启动，注入工作集 | `agent/session-start` | emit | `(payload: { agent: Agent; source: SessionStartSource }) => void` | `packages/core/agent/src/runtime-types.ts:217` |
| 注入文本到模型上下文 | `agent.inject(message)` | — | `inject(message: UserMessage): void` | `packages/core/agent/src/runtime-types.ts:143` |
| 工具调用后浮现（可附加上下文） | `tools/post-execute` | waterfall | `(exec: ToolExecution, result: Readonly<ToolExecutionResult>, next: () => Promise<PostToolDecision>) => Promise<PostToolDecision>` | `packages/core/tools/src/index.ts:175` |
| 工具调用后纯观察（不改结果） | `tools/result` | emit | `(exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>) => undefined` | `packages/core/tools/src/index.ts:197` |
| todo 状态变化（声明式事件源） | `session/event`，过滤 `event.type === 'todo/write'` | emit | `(session: Session, event: SessionEvent) => void`；`todo/write` 的 `data` 为 `{ todos: TodoItem[] }` | `packages/core/session/src/index.ts:76`；`packages/core/session/src/types.ts:302-303` |
| turn 边界 | `session/event`，过滤 `'turn/start'`／`'turn/end'` | emit | `data` 分别为 `{ turn: number }` 与 `{ turn: number; reason: TurnEndReason }` | `packages/core/session/src/types.ts:243,252` |
| step 边界 | `session/event`，过滤 `'step/start'`／`'step/end'` | emit | `data` 为 `{ turn: number; step: number }` | `packages/core/session/src/types.ts:254,256` |
| turn 结束前做可等待的工作 | `agent/turn-stopping` | serial（awaited） | `(payload: { agent: Agent; turn: number; signal: AbortSignal }) => Promise<void> \| void` | `packages/core/agent/src/runtime-types.ts:278` |
| compact 前抢救 flush | **未找到** | — | — | 见 2.2.1 |
| compact 事后观察 | `session/event`，过滤 `'compaction/start'` | emit（不可阻塞） | `data` 为 `{ compactionId: CompactionId; sourceCommandId?: CommandId; turn: number \| null }` | `packages/compaction/compaction/src/types.ts:22` |
| compact 后重新注入工作集 | `agent/session-start`，`source === 'compact'` | emit | `SessionStartSource = 'startup' \| 'resume' \| 'clear' \| 'compact'` | `packages/core/agent/src/runtime-types.ts:61` |
| 会话结束前的可等待 flush | `session/flush` | parallel（awaited） | `(session: Session) => Promise<void> \| void` | `packages/core/session/src/index.ts:87` |
| 会话／agent 退出观察 | `session/disposed`／`agent/disposed` | emit（返回的 promise 不被 await） | `(session: Session) => void`／`(payload: { agent: Agent }) => void` | `packages/core/session/src/index.ts:63`；`packages/core/agent/src/runtime-types.ts:168` |
| 插件卸载时的可等待清理 | `ctx.effect(execute, label?)` | — | `effect(execute: () => Effect, label?: string): AsyncDisposable<Promise<void>>` | `docs/cordis-api/fiber.md:24-25`；`vendor/cordis/src/fiber.ts:402-417` |
| 后台整理（dreaming） | `agent.runMaintenance(task)` | — | `runMaintenance<T>(task: (signal: AbortSignal) => Promise<T>): Promise<T>` | `packages/core/agent/src/runtime-types.ts:104` |
| LLM 抽取／整理调用 | `ctx.llm.stream(options)` | — | `stream(options: GenerateOptions): AsyncIterable<StreamChunk>` | `packages/llm/llm/src/index.ts:985` |

### 2.2.1 compact 前的抢救时机

**dsh 中不存在与 `PreCompact` 等价的可订阅扩展点。** 四条独立证据：

1. `compaction/start`／`compaction/summary`／`compaction/end`／`compaction/prune` 全部声明合并进 `SessionEventMap`，即会话日志事件，不是 Cordis `Events`（`packages/compaction/compaction/src/types.ts:16-23`）。`packages/compaction/compaction/src/index.ts:81-85` 中该包对 `@deepseek-ai/cordis` 的唯一合并是一个 `Context` 键，没有 `Events` 块。
2. 生成的扩展点目录 `docs/event-producer-consumer.md:10-68` 与 `packages/extensions/tool-cordis/src/api-catalog.ts:2367-2840` 的 `EVENT_API` 中都没有任何 `compaction/*` 条目。
3. `docs/subsystems/compaction.md:11` 明确这三个事件是 log-only：「All three are **log-only** — they record the lock, summary, selected range, shadowed event seqs, token count, and model call without joining the surface.」
4. `.agents/notes/implemented/feature/2026-06-30-interception-extension-points.md:50` 把 `PreCompact`／`PostCompact` 列为该设计的范围外。

三个部分替代方案：

| 方案 | 能力 | 限制 |
|---|---|---|
| `session/event` 过滤 `compaction/start` | 在锁已获取、摘要尚未生成、surface 尚未替换时收到通知 | emit 模式，不被等待，无法延迟或否决 compact |
| `agent/pre-step` 且监听器排在 compaction-basic 之前 | compaction-basic 的压力检查本身就是一个 `agent/pre-step` 监听器（`packages/compaction/compaction-basic/src/index.ts:147-165`），用 `ctx.on(name, listener, { prepend: true })`（`docs/cordis-api/events.md:181`）可抢在其前 | 只覆盖 `pressure` 触发路径。`context-overflow` 走 `agent/request-error`（`compaction-basic/src/index.ts:179`），`/compact` 命令的 `compactNow()` 两者都不走 |
| `agent/session-start` 且 `source === 'compact'` | compact 后会话重启时重新注入工作集 | 是事后补偿，不是事前抢救 |

对 eventmem 而言，这个缺口的实际严重度低于在 Claude Code 中：dsh 的会话日志是 append-only，compact 只是给区间打上 `surfaceOp: { op: 'replace', … }` 的影子标记，原事件仍留在日志里（`packages/session/session-persistence-jsonl/README.md:44`：「Append-only. Flushed events are never rewritten.」；`packages/compaction/compaction/README.md:43-47`）。**在 dsh 中，历史不会因为 compact 而丢失，抢救式 flush 的原始动机不成立。** 路线 B 的抽取应改为按 `session/event` 流式落盘，不再依赖「compact 前读一次 transcript」。

### 2.2.2 会话结束的时机

`session/end`、`agent/exit`、`agent/stop`、`agent/idle` 四个事件名均**未找到**。

可用组合：

| 时机 | 事件 | 是否等待异步工作 |
|---|---|---|
| 每 turn 收尾 | `agent/turn-stopping`（serial） | 是，被 `await this.dispatch.serial(...)` 等待（`packages/core/agent-loop/src/agent.ts:295`） |
| 持久化检查点 | `session/flush`（parallel） | 是，全部监听器被 await |
| agent 离开注册表 | `agent/disposed`（emit） | 否。实现把返回的 promise 交给 `void Promise.resolve(returned).catch(...)`（`packages/core/agent/src/index.ts:527-539`），异步工作不被等待 |
| 会话离开 store | `session/disposed`（emit） | 否 |
| 插件卸载 | `ctx.effect` 的 disposer | 是。async disposer 在 fiber 卸载时被 await（`vendor/cordis/src/fiber.ts:69-93`） |
| agent 空闲 | `agent/status` 且 `status === 'idle'`（`packages/core/agent/src/runtime-types.ts:178`），或 `agent.whenIdle()`（`:93`） | — |

推荐组合：机械 flush 挂 `session/flush`（awaited，语义匹配「落盘检查点」），两级整理挂 `agent/status → idle` 触发的 `agent.runMaintenance(...)`，插件级兜底清理挂 `ctx.effect` 的 async disposer。

### 2.2.3 工具调用后能拿到什么

`ToolExecution`（`packages/core/tools/src/index.ts:371-384` 继承 `ToolExecutionInput`，`:309-338`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `string` | 工具名 |
| `arguments` | `unknown` | 已解析入参，无损 JSON；由适配器自行收窄校验（`:322-323`） |
| `callId` | `CallId` | 调用标识 |
| `agent` | `Agent \| undefined` | 可能缺失，需保护式取值 |
| `signal` | `AbortSignal` | 取消信号，异步监听器必须观察 |

`ToolExecutionResult`（`packages/core/tools/src/index.ts:555-580`）是判别联合：

```ts
export interface ToolExecutionSuccess {
  readonly isError: false
  /** Execution-local canonical value; deliberately omitted from durable events. */
  readonly value: JsonValue
  readonly content: ContentBlock[]
  …
}
export interface ToolExecutionFailure {
  readonly isError: true
  readonly error: ToolFailure
  readonly content: ContentBlock[]
  …
}
```

对 eventmem 的直接意义：**Bash 的结构化结果在原生插件中可以拿到。** `bash` 工具的 `value` 形态见 `packages/shell/tool-bash/src/index.ts:158-181` 与输出 schema `:275-320`，含 `exitCode`、`timedOut`、`aborted`、`stdout: { text, truncated, spillPath? }`、`stderr: { … }`。这比 `_bash_error_text` 现在从 Claude Code 拿到的 `{"stdout","stderr","interrupted"}` 信息更完整（多了 `exitCode` 与 `timedOut`），错误签名的判定可以从关键词嗅探改为按 `isError`／`exitCode` 判定。

## 2.3 关键 API 签名

### 2.3.1 agent.inject

`packages/core/agent/src/runtime-types.ts:135-143`，逐字：

```ts
  /**
   * Queue model-facing context for the next pre-step without waking the
   * driver. A running driver claims it at the nearest later step boundary;
   * idle drivers leave it pending until follow-up or steering
   * wakes them. It may miss a request whose pre-step already claimed its
   * batch. Cancellation or disposal may discard pending context.
   * @param message - identified injected context and the source that supplied it.
   */
  inject(message: UserMessage): void
```

实现为 `this.send(input, 'next-step', false)`（`packages/core/agent-loop/src/agent.ts:130-132`）。与 `steer` 的唯一差别是 `wakeup` 布尔值：`steer` 为 `this.send(input, 'next-step', true)`（`agent.ts:126-128`），会唤醒空闲 driver。`followup` 为 `this.send(input, 'next-turn', true)`（`agent.ts:122-124`）。

消息落点是 agent inbox 的 `next-step` 边界，在下一次 `agent/pre-step` 被认领时进入模型上下文，并在会话日志中记为 `user/message`（`packages/core/session/src/types.ts:258-263`）。

### 2.3.2 消息构造与来源标记

`createUserMessage`（`packages/llm/llm/src/message.ts:192-199`）接受 `Omit<UserMessage, 'id' | 'role'>`（`:157`），即 `{ content: ContentBlock[]; source: MessageSource }`；`id` 自动生成，返回值被冻结。

`MessageSource`（`packages/llm/llm/src/message.ts:99-105`）：

```ts
export interface MessageSourceMap {
  user: { kind: 'user' }
  plugin: { kind: 'plugin'; plugin: string } & ContextFormed
  model: ModelMessageSource
  tool: ToolMessageSource
}
```

`ContextFormed`（`packages/llm/llm/src/message.ts:78-93`）的 `form` 可省略（`{ readonly form?: never }`），也可取 `'instructions'`／`'catalog'`／`'snapshot'`／`'notice'`／`'relay'`／`'recall'`。其中 `'recall'` 的定义为「Material lifted out of another session's log, possibly reduced on the way in.」（`:59`），与 eventmem 的浮现语义一致，建议采用：

```ts
const EVENTMEM_SOURCE: MessageSource = { kind: 'plugin', plugin: 'eventmem', form: 'recall' }
```

`'notice'` 形态要求同时给出不超过 120 个字符的 `summary`（`packages/llm/llm/src/message.ts:104,111`）。

### 2.3.3 ctx.llm

请求方法只有一个，`packages/llm/llm/src/index.ts:985`：

```ts
  stream(options: GenerateOptions): AsyncIterable<StreamChunk>
```

**非流式的 `generate()`／`complete()`：未找到。** 调用方用 `BlockAssembler`（导出于 `packages/llm/llm/src/index.ts:44`）把 chunk 收拢回 `ContentBlock[]`。

`GenerateOptions`（`packages/llm/llm/src/types.ts:341-377`）关键字段：

```ts
export interface GenerateOptions {
  provider: string
  model: string
  reasoningEffort?: ReasoningEffortId
  messages: Message[]
  system?: string
  tools?: ToolSchema[]
  temperature?: number
  maxTokens?: number
  stop?: string[]
  signal?: AbortSignal
  sessionId?: Branded<'SessionId'>
  purpose?: 'compaction' | 'session-title'
}
```

**指定便宜模型的方式是直接给 `provider` 与 `model` 两个字符串。** 没有命名 profile、模型别名或档位抽象。`smallModel`／`fastModel`／`cheapModel`／`backgroundModel` 等键在 dsh 中**未找到**；`LlmModelInfo`（`packages/llm/llm/src/types.ts:236-244`）只含 `id`、`name`、`description?`、`inputModalities?`，`LlmModelContext`（`:246-250`）只含 `contextWindow`，**没有价格或速度字段**，因此模型选择只能是配置决策。

仓库内的选路约定为「插件自带一对可选的 `provider` + `model` 配置，缺省时回退到最近一次已记录的请求路由，再回退到 agent 自身的路由」。参照实现 `packages/compaction/compaction-basic/src/summarizer.ts:128-143`：

```ts
  const latest = agent.session.requestHeader()?.config
  const configured = config.summarizationProvider.length === 0
    ? undefined
    : { provider: config.summarizationProvider, model: config.summarizationModel }
  const agentTarget = agent.options.provider !== undefined
    && agent.options.provider.length > 0
    && agent.options.model !== undefined
    && agent.options.model.length > 0
    ? { provider: agent.options.provider, model: agent.options.model }
    : undefined
  const target = configured ?? latest ?? agentTarget
```

`session-title-llm` 采用同一模式，并要求 `provider` 与 `model` 成对提供（`packages/session/session-title-llm/src/index.ts:62-65,130`）。

`purpose` 是一个**封闭联合**，只有 `'compaction'` 与 `'session-title'` 两个成员（`packages/llm/llm/src/types.ts:376`），不是可合并扩展的映射。若 eventmem 需要独立的 purpose 标签，需要上游改动 `dsh-llm` 的该联合类型。副请求不经过 `agent/request`，只经过 `llm/stream` waterfall（`packages/compaction/compaction/README.md:23`）。

### 2.3.4 ctx.jobs

服务键确为 `jobs`（`packages/jobs/jobs/src/index.ts:29-33`）。注册方式为 `abstract start(spec: JobStart): JobId`（`packages/jobs/jobs/src/index.ts:82`）。`JobStart`（`packages/jobs/jobs/src/types.ts:41-69`）含 `kind`、`label`、`outputLimitBytes?`、`owner?: Agent`、`run(): JobHooks`；`JobHooks`（`:71-91`）含 `cancel(reason?)`、`done: Promise<JobOutcome>`、`readOutput?()`。

三项限制使 `ctx.jobs` 不适合承载 dreaming：

| 限制 | 说明 | 源码 |
|---|---|---|
| 没有调度能力 | `JobStart` 无时间字段，`run()` 在预检后立即调用 | `packages/jobs/jobs/src/types.ts:41-69` |
| 不跨进程持久化 | 「Jobs are process-local — records die with the harness process」 | `packages/jobs/jobs-local/README.md:33` |
| 需要挂载控制器 | 无控制器时 `start()` 拒绝：`background jobs unavailable: no job controller serves this agent (load @deepseek-ai/dsh-tool-jobs in its composition)` | `packages/jobs/jobs-local/README.md:21` |

`ctx.jobs` 的定位是模型可见的后台作业（配套 `job_list`／`job_output`／`job_kill` 三个工具）。dreaming 对模型不可见，因此推荐 `agent.runMaintenance(task)`（`packages/core/agent/src/runtime-types.ts:95-104`，「Run one non-turn maintenance task from the true idle phase」），或裸 Promise 加 `ctx.effect` 的 async disposer 做退出兜底。

`packages/schedule/` 是时间触发的另一套机制，但**不暴露任何服务**（`packages/schedule/README.md:11`；`packages/schedule/schedule/src/index.ts` 无 `declare module '@deepseek-ai/cordis'`），只提供 `schedule_create`／`schedule_list`／`schedule_delete` 三个模型可见工具，插件无法编程调用。

### 2.3.5 compaction-tool-result-pruner 与「工具结果清退」

包名 `@deepseek-ai/dsh-compaction-tool-result-pruner`，目录 `packages/compaction/compaction-tool-result-pruner/`。

它做的事（`README.md:5`）：把超预算的 `tool/result` surface 节点重写为「有界头部 ＋ 固定省略标记 ＋ 有界尾部」，同时在 append-only 会话日志中保留完整原事件。判定纯按字符预算，不调用模型（`src/index.ts:83-85`），配置为 `thresholdChars`（默认 8192）、`headChars`（默认 4096）、`tailChars`（默认 1024）（`src/types.ts:4-11`；`src/config.ts:7-14`）。

它**不订阅任何扩展点**：是 `Service` 子类，没有 `apply()`（`src/index.ts:44-61`），由 compaction-basic 通过 `this.ctx.get('toolResultPruner')` 拉取式调用，且仅在 `trigger === 'context-overflow'` 时（`packages/compaction/compaction-basic/src/index.ts:281-287`）。

与 eventmem 的关系：**不可直接复用。** 两者的动机重合（都在压缩工具结果），但 pruner 的判据是字符数、产出是截断文本、写入目标是会话 surface；eventmem 的判据是事件闭合、产出是 outcome 单行、写入目标是 `.memory/`。可借鉴的是它的写法范式——append 一条影子计价事件再 append 带 `surfaceOp: { op: 'replace', … }` 的替换事件（`src/index.ts:162-173`）——若 eventmem 未来要在 dsh 内直接替换 surface 节点，这是唯一有先例的做法。

### 2.3.6 todo 事件源

**`todo/*` Cordis 事件：未找到。** todo 状态变化只有会话日志事件 `todo/write`（`packages/core/session/src/types.ts:302-303`）：

```ts
  /** Whole-list snapshot; latest write wins on replay. Log-only UI state; never derived history. */
  'todo/write': { todos: TodoItem[] }
```

`TodoItem`（`packages/core/session/src/types.ts:188-194`）：

```ts
export interface TodoItem {
  /** What this task is — a short imperative line shown in the UI. */
  content: string
  /** Lifecycle state. `in_progress` marks a task being worked now; parallel work may mark several. */
  status: 'pending' | 'in_progress' | 'completed'
}
```

写入位置在 agent 自己的会话日志，别无他处（`packages/todo/tool-todo/src/index.ts:213`：`exec.agent.session.append('todo/write', { todos })`）。订阅方式为 `session/event` 加类型过滤，`packages/todo/tool-todo/README.md:29` 确认这就是预期的消费模型：「UIs subscribe to the event stream and render that durable list themselves」。

与 Claude Code 的 `TodoWrite` 相比缺 `activeForm` 字段（类型注释 `:181-186` 说明是刻意省略），eventmem 的 `_handle_todo_write` 已有 `content` 优先的取值顺序，不受影响。

## 2.4 插件骨架

以下骨架的每一处 API 都对应 2.1–2.3 的引用，可直接抄改。

`package.json`：

```json
{
  "name": "dsh-eventmem",
  "version": "0.1.0",
  "type": "module",
  "main": "lib/index.js",
  "types": "lib/types/index.d.ts",
  "exports": {
    ".": { "types": "./lib/types/index.d.ts", "default": "./lib/index.js" },
    "./package.json": "./package.json"
  },
  "files": ["lib", "cordis.patch.yml"],
  "license": "MIT",
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } },
  "dependencies": { "@deepseek-ai/schemastery": "^3.18.1" },
  "peerDependencies": {
    "@deepseek-ai/cordis": "^4.0.1",
    "@deepseek-ai/dsh-agent": "^0.1.1-rc.2",
    "@deepseek-ai/dsh-llm": "^0.1.1-rc.2",
    "@deepseek-ai/dsh-session": "^0.1.1-rc.2",
    "@deepseek-ai/dsh-tools": "^0.1.1-rc.2"
  }
}
```

`cordis.patch.yml`：

```yaml
- insert:
    - id: eventmem
      name: dsh-eventmem
      config:
        surfaceK: 3
```

`src/index.ts`：

```ts
/**
 * eventmem：基于事件的 agent 记忆系统，dsh 宿主适配。
 * @module dsh-eventmem
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { MessageSource, UserMessage } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { PostToolDecision, ToolExecution, ToolExecutionResult } from '@deepseek-ai/dsh-tools'

export const name = 'eventmem'
// `llm` 供后台整理调用；其余能力经 payload 传入，不需要注入。
export const inject = ['llm']

/** 插件配置。 */
export interface Config {
  /** 单次触发注入的最大行数。 */
  surfaceK: number
  /** 记忆目录名，默认 `.memory`；与 Python 版逐字节兼容的格式契约。 */
  memoryDirName?: string
  /** 后台整理使用的 provider 路由；必须与 `model` 成对提供。 */
  provider?: string
  /** 后台整理使用的模型 id；必须与 `provider` 成对提供。 */
  model?: string
}

export const Config: z<Config> = z.object({
  surfaceK: z.number().step(1).min(1).default(3),
  memoryDirName: z.string().default('.memory'),
  provider: z.string(),
  model: z.string(),
})

/** 注入消息的来源标记；`recall` 形态即「从别处日志提取的材料」。 */
const EVENTMEM_SOURCE: MessageSource = { kind: 'plugin', plugin: 'eventmem', form: 'recall' }

export function apply(ctx: Context, config: Config): void {
  // ---- 会话启动：注入工作集 ----
  // `source` 取值为 'startup' | 'resume' | 'clear' | 'compact'
  // （packages/core/agent/src/runtime-types.ts:61）。compact 后会重新触发，
  // 因此这一条同时承担了 PostCompact 的补偿注入。
  ctx.on('agent/session-start', ({ agent, source }) => {
    const text = readWorkingSet(memoryRoot(agent, config))
    if (text.length === 0) return
    agent.inject(message(text))
  })

  // ---- 工具调用后：浮现 ----
  // waterfall：必须调用 next() 让后续监听器仍能改写或阻断，
  // 再把自己的上下文折进下游决策（参照 hooks-claude-code/src/index.ts:256-264）。
  ctx.on('tools/post-execute', async (
    exec: ToolExecution,
    result: Readonly<ToolExecutionResult>,
    next: () => Promise<PostToolDecision>,
  ): Promise<PostToolDecision> => {
    const hits = surface(exec, result, config)
    const downstream = await next()
    if (hits.length === 0) return downstream
    const ours = message(`Memory:\n${hits.join('\n')}`)
    return { ...downstream, additionalContexts: [ours, ...downstream.additionalContexts ?? []] }
  })

  // ---- 会话日志流：声明式事件源 ----
  // turn/step/todo/compaction 边界都只在这条 emit 通路上可见。
  ctx.on('session/event', (session: Session, event: SessionEvent) => {
    switch (event.type) {
      case 'todo/write':
        // event.data: { todos: TodoItem[] }
        // packages/core/session/src/types.ts:302-303
        onTodoWrite(session, event.data.todos)
        break
      case 'turn/end':
        // event.data: { turn: number; reason: TurnEndReason }
        onTurnEnd(session, event.data.turn)
        break
      case 'compaction/start':
        // 仅为通知，不能延迟或否决 compact。
        // dsh 的会话日志 append-only，compact 不删除历史，
        // 因此这里不需要抢救式 flush。
        onCompactionStart(session)
        break
      default:
        break
    }
  })

  // ---- 持久化检查点：机械 flush ----
  // parallel 模式，全部监听器被 await，可以做同步落盘。
  ctx.on('session/flush', async (session: Session) => {
    await flushMechanical(session)
  })

  // ---- 空闲整理：dreaming ----
  ctx.on('agent/status', ({ agent, status }) => {
    if (status !== 'idle') return
    if (!isDirty(memoryRoot(agent, config))) return
    void agent.runMaintenance(async (signal) => {
      await consolidate(ctx, agent, config, signal)
    }).catch((error: unknown) => {
      ctx.logger.warn(`eventmem: consolidate failed: ${String(error)}`)
    })
  })

  // ---- 卸载兜底：async disposer 在 fiber 卸载时被 await ----
  ctx.effect(() => async () => {
    await drainPendingWrites()
  }, 'eventmem: drain pending writes')
}

/** 把一段文本包成带来源标记的用户消息。 */
function message(text: string): UserMessage {
  return createUserMessage({ content: [{ type: 'text', text }], source: EVENTMEM_SOURCE })
}

/** 记忆根目录：会话工作目录下的 `.memory`。 */
function memoryRoot(agent: Agent, config: Config): string {
  const cwd = agent.session.header.cwd ?? process.cwd()
  return `${cwd}/${config.memoryDirName ?? '.memory'}`
}
```

未在骨架中展开的函数（`readWorkingSet`／`surface`／`onTodoWrite`／`onTurnEnd`／`onCompactionStart`／`flushMechanical`／`isDirty`／`consolidate`／`drainPendingWrites`）是 eventmem 自身的逻辑，与宿主无关。

后台整理内部发起模型请求的形态（参照 `packages/compaction/compaction-basic/src/summarizer.ts:153-164`）：

```ts
import { BlockAssembler } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions } from '@deepseek-ai/dsh-llm'

const target = pickRoute(agent, config)  // configured ?? latest ?? agentTarget
const options: GenerateOptions = {
  provider: target.provider,
  model: target.model,
  messages: [createUserMessage({ content: [{ type: 'text', text: prompt }], source: EVENTMEM_SOURCE })],
  maxTokens: 4096,
  sessionId: agent.session.id,
  signal,
}
const assembler = new BlockAssembler()
for await (const chunk of ctx.llm.stream(options)) assembler.push(chunk)
const blocks = assembler.blocks()
```

## 2.5 `.memory/` 兼容性结论

### 2.5.1 路径与冲突

`<project>/.memory/` 在 dsh 中**无冲突**：仓库范围内对 `.memory` 的引用为零（`grep -rn "'\.memory'\|\.memory/" packages/ examples/ apps/` 无结果）。dsh 自己的持久化落在配置的 `root` 下，基础组合把它指向 `$DSH_HOME/sessions`（`packages/bundle/base/cordis.patch.yml:98-101`）。

项目根取 `agent.session.header.cwd`（`packages/core/session/src/types.ts:73`），与 hooks-claude-code 把该值作为 `cwd` 传给钩子进程的做法一致（`packages/hooks/hooks-claude-code/src/index.ts:147,328`）。

### 2.5.2 不使用 ctx.storage

`ctx.storage` 是后端注册表加数据形态挂载的抽象（`packages/storage/storage/src/index.ts:47-90`），JSON 后端把文件写成 `join(root, '<unit>.json')`（`packages/storage/storage-json/src/index.ts:64-65`），域形态还要求每条记录有 zod schema（`packages/storage/storage-domain/README.md`，`packages/storage/storage-domain/src/index.ts:100`）。这套抽象与 `.memory/` 的 markdown 目录树不兼容，且会把文件位置交给部署配置，破坏「记忆跟随项目」的前提。

**结论：直接用 `node:fs`。** 有先例：`hooks-claude-code` 自身就用 `readFileSync`（`packages/hooks/hooks-claude-code/src/index.ts:12,104`），`storage-json` 用 `node:fs/promises` 的 `mkdir`（`:8-9`）。

需要注意 `AGENTS.md:113` 的规则：「No hardcoded tunables in plugins: deployment-varying choices are validated `Config` fields」。`.memory` 这个目录名不是部署可变量，而是与 Python 版的格式契约，因此写成带默认值的 `Config` 字段（`memoryDirName`）即可，不必要求必填。

### 2.5.3 逐字节兼容的具体要求

`paths.py` 的布局是纯字符串拼接，TS 侧照抄即可（`src/eventmem/paths.py:33-99`）。真正需要显式对齐的是四处：

| 项目 | Python 现状 | TS 侧要求 | 风险 |
|---|---|---|---|
| 原子写 | 同目录 `mkstemp` ＋ `os.replace`，`newline="\n"`，UTF-8（`paths.py:117-127`） | 同目录临时文件 ＋ `fs.renameSync`；写入前把 `\r\n` 归一为 `\n` | 低 |
| 缩进 JSON（`index/anchors.json`、lesson 状态、todo 状态） | `json.dumps(x, ensure_ascii=False, indent=2)`（`index.py:185`；`consolidate.py:417`；`extract.py:194`） | `JSON.stringify(x, null, 2)` | **无**：已实测逐字节一致，含空数组 `[]`、空对象 `{}`、非 ASCII 键三类边界 |
| 紧凑 JSON（`log/todo-observed.jsonl`、钩子 stdout） | `json.dumps(x, ensure_ascii=False)`，默认分隔符为 `', '` 与 `': '`，**带空格** | `JSON.stringify(x)` **不带空格** | **高**：这是确定的逐字节差异。若要求该文件逐字节兼容，TS 侧需自行按 Python 的默认分隔符序列化 |
| 键序 | Python dict 保持插入序；`_write_anchor_map` 显式 `sorted()`（`index.py:184`） | JS 对象对「类整数」字符串键会按数值重排 | 中：锚点键形如 `file:src/foo.py`，不触发重排；但新增键格式时需复核 |
| 排序 | Python `sorted()` 按 Unicode 码位 | JS `Array.prototype.sort()` 默认按 UTF-16 码元 | 低：仅在星光平面字符参与排序时不同；文件路径场景不触发 |

`ensure_ascii=False` 与 JS 的默认行为一致（都输出原始 UTF-8），差异仅出现在孤立代理项上。

### 2.5.4 结论

`.memory/` 与 Python 版逐字节兼容**可以达成**，成本集中在紧凑 JSON 的分隔符一项；其余是照抄路径拼接与原子写。没有目录冲突，不需要引入 dsh 的存储抽象。

## 2.6 推荐路线：B-lite

路线 B 的完整实现要把 `store.py`（172 行）、`index.py`（284 行）、`recall.py`（205 行）、`schema.py`（282 行）共约 940 行的纯逻辑移植到 TS，另有 `extract.py`（932 行）、`consolidate.py`（576 行）、`llm.py`（272 行）的 LLM 相关部分。两套实现并存会带来持续的逐字节漂移风险。

一个更小的切法：**TS 插件只做宿主适配，不移植逻辑。**

| 层 | 归属 | 做什么 |
|---|---|---|
| 同步读路径（浮现） | TS | 读 `index/anchors.json` ＋ `index/project.md`，纯查表，毫秒级，不调模型。这部分逻辑最简单，是 `recall.py` 中不含 BM25 的那一半 |
| 写入与整理 | Python，由 TS 通过子进程拉起 | TS 侧在 `session/event`／`session/flush` 时把结构化事件写成中间 JSONL，再 spawn `python3 -m eventmem.cli ...`，与现有 `spawn_detached`（`src/eventmem/hooks/__init__.py:103-130`）的形态一致 |
| `.memory/` 写入 | 全部由 Python 独占 | 消除逐字节漂移风险 |

代价是宿主机需要 Python 运行时。收益是 `.memory/` 只有一个写入方，且 `extract.py` 的输入从「解析 Claude Code transcript」改为「读结构化事件 JSONL」，反而比现状更可靠——因为 dsh 的 `tool/result` 带 `isError` 与结构化 `value`，`todo/write` 带完整快照，都不需要防御式解析。

---

# 第三节 风险

## 3.1 API 稳定性

`README.md:11`，逐字：

> DeepSeek Harness is currently in _developer preview_ and is iterating rapidly. **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**

当前版本 `0.1.1-rc.2`。仓库内部所有 peer 依赖一律写 `workspace:^`，**第三方可依赖的稳定版本区间：未找到**。

## 3.2 源码中标注的未完成项

在与本适配相关的包内检索 `TODO(`：

| 标记 | 位置 | 对 eventmem 的影响 |
|---|---|---|
| `TODO(session-start-gating)` | `packages/hooks/hooks-claude-code/src/index.ts:205` | 路线 A：工作集注入可能错过第一次模型请求 |
| `TODO(per-session-hook-config)` | `packages/hooks/hooks-claude-code/src/index.ts:50` | 路线 A：单进程内无法按项目区分钩子配置 |
| `TODO(hook-continue-false)` | `packages/hooks/hooks-claude-code/src/index.ts:189` | 无影响（eventmem 不返回 `continue: false`） |
| `TODO(stop-loop-guard)` | `packages/hooks/hooks-claude-code/src/index.ts:269` | 仅在采用 1.6 的 `Stop` 替代方案时相关；eventmem 不返回阻塞决策，不受影响 |

`packages/core/agent/`、`packages/core/tools/`、`packages/core/session/`、`packages/compaction/`、`packages/todo/`、`packages/jobs/` 六个包内**没有** `TODO(` 标记（同一次检索无结果）。路线 B 依赖的扩展点均在这些包内。

## 3.3 其他风险

| 编号 | 风险 | 说明 | 缓解 |
|---|---|---|---|
| R-1 | `agent.inject` 不保证送达 | 「It may miss a request whose pre-step already claimed its batch. Cancellation or disposal may discard pending context.」（`packages/core/agent/src/runtime-types.ts:139-140`） | 关键注入改用 `agent.steer`（会唤醒 driver），代价是可能多起一个 turn |
| R-2 | compact 前无扩展点 | 见 2.2.1；且 `PreCompact` 是显式的范围外决策，不是待办项，短期内不会出现 | 依赖 dsh 会话日志 append-only 的性质，改为流式落盘 |
| R-3 | `session/event` 是 emit，失败被吞 | 「observer failures are logged and contained without making the committed append fail」（`packages/core/session/src/index.ts:65-68`） | 监听器内自行做错误上报；关键写入放到 `session/flush`（被 await） |
| R-4 | `agent/disposed`／`session/disposed` 不等待异步工作 | 返回的 promise 被 `void Promise.resolve(returned).catch(...)` 丢弃（`packages/core/agent/src/index.ts:527-539`） | 退出时的必须完成工作放 `ctx.effect` 的 async disposer |
| R-5 | `exec.agent` 可能为 `undefined` | `ToolExecutionInput.agent?: Agent`（`packages/core/tools/src/index.ts:325`）；hooks-claude-code 两处都做了保护（`src/index.ts:240,249`） | 保护式取值，缺失时跳过本次浮现 |
| R-6 | `exec.arguments` 类型为 `unknown` | 「Losslessly JSON-serializable parsed arguments (tools validate their own schema)」（`packages/core/tools/src/index.ts:322-323`） | 适配器自行收窄校验，不假设字段存在 |
| R-7 | 工具名与入参 schema 由部署的工具包决定 | `read`／`edit`／`write` 来自 `@deepseek-ai/dsh-tool-fs`；同一能力另有 `str_replace_editor`（`@deepseek-ai/dsh-tool-str-replace-editor`，`docs/tool-catalog.md:26`）等替代实现，字段名不同 | 工具名与字段映射做成配置，不硬编码 |
| R-8 | 会话持久化后端影响 `transcript_path` | JSONL 后端返回路径，SQLite 后端返回 `undefined`（`packages/session/session-persistence-sqlite/src/index.ts:93`） | 路线 B 不读 transcript，不受影响；路线 A 受影响 |
| R-9 | `purpose` 是封闭联合 | `'compaction' | 'session-title'` 两个成员（`packages/llm/llm/src/types.ts:376`），不可合并扩展 | 后台整理请求不打 purpose 标签，或提交上游改动 |
| R-10 | bundle 成员变更需要重启 | 「a running Profile keeps the Bundle set from its current start」（`apps/cli/reference/README.md:55`） | 开发期用 `--patch` 加绝对路径加载，走热重载 |
| R-11 | git host 安装需要用户放行构建脚本 | 「permission to execute the package's code on your machine at install time, outside any sandbox the agent runs under」（`docs/user/develop/basic/publish.md:173`） | 走 npm 或 `pnpm pack` 的 tarball，两者不需要构建许可 |

## 3.4 两个 README 与实现不一致处

| 位置 | 不一致内容 | 以哪个为准 |
|---|---|---|
| `packages/hooks/hooks-claude-code/README.md:96` 称 payload 省略 `transcript_path`，`:51` 称每个 agent 作用域 payload 都带 `transcript_path` | 实现 `src/index.ts:322-331` 的 `base()` 包含该字段 | 实现。原因未查明 |
| `README.md:94` 称 `SubagentStart` 省略 `transcript_path` | `subagentPayload` 同样调用 `base()`（`src/index.ts:354-361`），child 为 `undefined` 时该字段为空串 | 实现；「省略」应理解为「取空值」 |

---

# 附：核心源码引用索引

| 主题 | 路径 | 行 |
|---|---|---|
| CC 钩子事件白名单 | `packages/hooks/hooks-claude-code/src/config.ts` | 11-19 |
| 钩子点到扩展点的注册 | `packages/hooks/hooks-claude-code/src/index.ts` | 206, 219, 238, 247, 270, 281, 291 |
| 钩子 stdin 基础字段 | `packages/hooks/hooks-claude-code/src/index.ts` | 322-331 |
| PostToolUse payload | `packages/hooks/hooks-claude-code/src/index.ts` | 342-344 |
| 内容块拍平 | `packages/hooks/hooks-claude-code/src/index.ts` | 318-320 |
| 桥接 Config | `packages/hooks/hooks-claude-code/src/index.ts` | 45-78 |
| matcher 字面量语义 | `packages/hooks/hook-protocol/src/matcher.ts` | 18, 57-65 |
| 结构化 stdout 解码 | `packages/hooks/hook-protocol/src/codec.ts` | 59-89, 97-134 |
| 未支持钩子清单 | `packages/hooks/hooks-claude-code/README.md` | 89 |
| JSONL transcript 布局 | `packages/session/session-persistence-jsonl/README.md` | 5, 17, 18, 36, 44 |
| `locate()` 实现 | `packages/session/session-persistence-jsonl/src/index.ts` | 172-174 |
| SQLite 后端 `locate()` 返回 undefined | `packages/session/session-persistence-sqlite/src/index.ts` | 93 |
| `SessionEventMap` | `packages/core/session/src/types.ts` | 236-336 |
| `SessionEvent` 信封 | `packages/core/session/src/types.ts` | 408-415 |
| `SessionHeader` | `packages/core/session/src/types.ts` | 61-91 |
| `TodoItem` | `packages/core/session/src/types.ts` | 179-194 |
| `session/*` Cordis 事件 | `packages/core/session/src/index.ts` | 37-88 |
| `Agent` 接口 | `packages/core/agent/src/runtime-types.ts` | 63-144 |
| `agent/*` Cordis 事件 | `packages/core/agent/src/runtime-types.ts` | 146-292 |
| `SessionStartSource` | `packages/core/agent/src/runtime-types.ts` | 61 |
| `tools/*` Cordis 事件 | `packages/core/tools/src/index.ts` | 137-212 |
| `ToolExecution` 系列类型 | `packages/core/tools/src/index.ts` | 309-394 |
| `ToolExecutionResult` | `packages/core/tools/src/index.ts` | 555-580 |
| `PreToolDecision`／`PostToolDecision` | `packages/core/tools/src/index.ts` | 582-600 |
| bash 结构化结果 | `packages/shell/tool-bash/src/index.ts` | 158-181, 275-320 |
| `compaction/*` 会话事件 | `packages/compaction/compaction/src/types.ts` | 16-70 |
| compaction-basic 的 pre-step 监听 | `packages/compaction/compaction-basic/src/index.ts` | 147-165 |
| 摘要请求的选路 | `packages/compaction/compaction-basic/src/summarizer.ts` | 128-164 |
| tool-result-pruner 服务 | `packages/compaction/compaction-tool-result-pruner/src/index.ts` | 44-61, 136-173 |
| `ctx.llm.stream` | `packages/llm/llm/src/index.ts` | 985 |
| `GenerateOptions` | `packages/llm/llm/src/types.ts` | 341-377 |
| `MessageSource`／`ContextFormed` | `packages/llm/llm/src/message.ts` | 63-126 |
| `createUserMessage` | `packages/llm/llm/src/message.ts` | 186-199 |
| `JobRegistry` | `packages/jobs/jobs/src/index.ts` | 62-176 |
| `JobStart`／`JobHooks` | `packages/jobs/jobs/src/types.ts` | 41-91 |
| todo 写入 | `packages/todo/tool-todo/src/index.ts` | 206-213 |
| 插件形态硬规则 | `packages/AGENTS.md` | 5, 6, 26-27 |
| 第三方插件打包与发布 | `docs/user/develop/basic/publish.md` | 26-62, 106, 160-178 |
| 配置层叠顺序 | `apps/cli/reference/README.md` | 9, 11, 43, 55 |
| `ctx.effect` | `docs/cordis-api/fiber.md` | 24-25 |
| 分发模式语义 | `docs/cordis-api/events.md` | 193-204 |
| 扩展点范围外决策 | `.agents/notes/implemented/feature/2026-06-30-interception-extension-points.md` | 50 |
| developer preview 声明 | `README.md` | 11 |
| `dsh-plugin` topic | `README.md` | 42 |
