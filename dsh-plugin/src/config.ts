/**
 * 插件配置。
 *
 * 工具名与入参字段名由部署装载的工具包决定（`read`/`edit`/`write` 来自
 * `@deepseek-ai/dsh-tool-fs`，同一能力另有 `str_replace_editor` 等替代实现），
 * 因此一律做成配置而非硬编码（AGENTS.md:113「No hardcoded tunables in plugins」）。
 *
 * @module
 */

import z from '@deepseek-ai/schemastery'

/** 一个工具名的处理方式。 */
export type ToolRole = 'file' | 'error' | 'todo'

/** 插件配置。 */
export interface Config {
  /** 总开关。关闭后所有监听器立即返回。 */
  enabled: boolean
  /** 单次浮现注入的最大行数（DESIGN §5 的 surface_k）。 */
  surfaceK: number
  /** 记忆目录名；与 Python 版的格式契约，不是部署可变量。 */
  memoryDirName: string
  /** Python 可执行程序，用于 spawn `-m eventmem.cli`。 */
  pythonExecutable: string
  /** Python 模块名，默认 `eventmem.cli`。 */
  pythonModule: string
  /** agent 连续 idle 多少秒后触发整理。 */
  idleDebounceSeconds: number
  /** 是否在 `agent/session-start` 注入工作集。 */
  injectWorkingSet: boolean
  /** 是否把机械事实写成 feed jsonl。 */
  writeFeed: boolean
  /** 是否在 idle 时 spawn Python 做抽取与整理。 */
  runMaintenance: boolean
  /** `.memory/` 不存在时是否 spawn `eventmem init` 自举。 */
  bootstrap: boolean
  /** 工具名到处理方式的映射；未列出的工具名不参与浮现。 */
  toolRoles: Record<string, ToolRole>
  /** 工具名到 Claude Code 形态工具名的映射，供 feed 转写使用。 */
  toolNameMap: Record<string, string>
  /** 文件类工具中依次尝试的路径入参字段名。 */
  filePathKeys: string[]
  /** bash 类工具中命令入参的字段名。 */
  commandKeys: string[]
  /**
   * 命中即视为委托子 agent 调用的工具名单（小写比较），写入 feed 供 Python
   * `extract.py` 的委托事件识别消费（DESIGN/SPEC §3.17）。
   */
  delegationTools: string[]
}

/**
 * 出厂默认的工具角色表。
 *
 * 工具名取自 `docs/tool-catalog.md`：`read`／`read_image`／`edit`／`write` 来自
 * `@deepseek-ai/dsh-tool-fs`（入参 `file_path`），`str_replace_editor` 来自同名包
 * （入参 `path`），`bash` 来自 `@deepseek-ai/dsh-tool-bash`（入参 `command`），
 * `todo_write` 来自 `@deepseek-ai/dsh-tool-todo`。
 *
 * `todo` 角色表示「在 `tools/result` 上跳过」——todo 快照统一由 `session/event`
 * 的 `todo/write` 处理，避免同一次写入被记两遍。
 */
export const DEFAULT_TOOL_ROLES: Record<string, ToolRole> = {
  read: 'file',
  read_image: 'file',
  edit: 'file',
  write: 'file',
  str_replace_editor: 'file',
  bash: 'error',
  pwsh: 'error',
  todo_write: 'todo',
}

/** 出厂默认的工具名翻译表：dsh 工具名 → `extract.py` 认识的 Claude Code 工具名。 */
export const DEFAULT_TOOL_NAME_MAP: Record<string, string> = {
  read: 'Read',
  read_image: 'Read',
  edit: 'Edit',
  write: 'Write',
  str_replace_editor: 'Edit',
  bash: 'Bash',
  pwsh: 'Bash',
  todo_write: 'TodoWrite',
}

/**
 * 出厂默认的委托工具名单（小写比较）。命中时整个调用以 assistant tool_use ＋
 * user tool_result 的形态写进 feed，工具名首字母大写化（`task`→`Task`），供
 * Python `extract.py` 的委托事件识别消费（SPEC §3.17）。
 */
export const DEFAULT_DELEGATION_TOOLS: string[] = ['task', 'subagent', 'agent']

/**
 * 配置 schema。所有字段带默认值，最小可用配置是空对象，因此入参类型是
 * `Partial<Config>`——这与 dsh 仓库内插件惯用的 `z<Config>` 的差别仅在入参侧：
 * 那些插件的字段用 `.required()`，没有默认值可退。
 */
export const Config: z<Partial<Config>, Config> = z.object({
  enabled: z.boolean().default(true).description('总开关'),
  surfaceK: z.natural().min(1).max(20).default(3).description('单次浮现注入的最大行数'),
  memoryDirName: z.string().default('.memory').description('记忆目录名，与 Python 版的格式契约'),
  pythonExecutable: z.string().default('python3').description('Python 可执行程序'),
  pythonModule: z.string().default('eventmem.cli').description('Python 模块名'),
  idleDebounceSeconds: z.natural().min(1).default(30).description('连续 idle 多少秒后触发整理'),
  injectWorkingSet: z.boolean().default(true).description('会话启动时注入工作集'),
  writeFeed: z.boolean().default(true).description('把机械事实写成 feed jsonl'),
  runMaintenance: z.boolean().default(true).description('idle 时 spawn Python 做抽取与整理'),
  bootstrap: z.boolean().default(true).description('.memory/ 不存在时自举'),
  toolRoles: z.dict(z.union(['file', 'error', 'todo'] as const))
    .default(DEFAULT_TOOL_ROLES)
    .description('工具名到处理方式的映射'),
  toolNameMap: z.dict(z.string())
    .default(DEFAULT_TOOL_NAME_MAP)
    .description('工具名到 Claude Code 形态工具名的映射，供 feed 转写使用'),
  filePathKeys: z.array(z.string())
    .default(['file_path', 'notebook_path', 'path'])
    .description('文件类工具中依次尝试的路径入参字段名'),
  commandKeys: z.array(z.string())
    .default(['command', 'cmd'])
    .description('bash 类工具中命令入参的字段名'),
  delegationTools: z.array(z.string())
    .default(DEFAULT_DELEGATION_TOOLS)
    .description('命中即视为委托子 agent 调用的工具名单（小写比较）'),
})
