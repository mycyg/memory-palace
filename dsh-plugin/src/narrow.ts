/**
 * 对 `exec.arguments`（类型 `unknown`）与 `result.value`（类型 `JsonValue`）的收窄校验。
 *
 * 工具自己校验各自的 schema，注册表不代为收窄（`packages/core/tools/src/index.ts:322-323`），
 * 因此这里一律保护式取值，不假设任何字段存在。
 *
 * @module
 */

import type { FeedBashResult, FeedTodo } from './feed.js'

/**
 * 收窄成普通对象。
 *
 * @param value - 任意取值。
 * @returns 对象本体，或 undefined。
 */
export function asObject(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

/**
 * 取一个非空字符串字段。
 *
 * @param source - 对象。
 * @param key - 字段名。
 * @returns 去除两端空白后的字符串，或 undefined。
 */
export function asText(source: Record<string, unknown> | undefined, key: string): string | undefined {
  if (source === undefined) return undefined
  const value = source[key]
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/**
 * 按候选字段名依次尝试取第一个非空字符串。
 *
 * @param source - 对象。
 * @param keys - 候选字段名，按顺序尝试。
 * @returns `[字段名, 取值]`，全部落空时返回 undefined。
 */
export function firstText(
  source: Record<string, unknown> | undefined,
  keys: readonly string[],
): [string, string] | undefined {
  for (const key of keys) {
    const value = asText(source, key)
    if (value !== undefined) return [key, value]
  }
  return undefined
}

/**
 * 取 todo 快照。
 *
 * @param todos - `todo/write` 事件的 `data.todos` 或 todo 工具的入参。
 * @returns 规约后的 todo 列表；无有效条目时为空数组。
 */
export function asTodos(todos: unknown): FeedTodo[] {
  if (!Array.isArray(todos)) return []
  const out: FeedTodo[] = []
  for (const raw of todos) {
    const item = asObject(raw)
    if (item === undefined) continue
    const content = asText(item, 'content') ?? asText(item, 'activeForm') ?? asText(item, 'task')
    if (content === undefined) continue
    const status = (asText(item, 'status') ?? 'pending').toLowerCase()
    out.push({ content, status })
  }
  return out
}

/**
 * 从 bash 工具的结构化结果里取退出码与两条流。
 *
 * 形态见 `packages/shell/tool-bash/src/index.ts:158-181`：
 * `{ exitCode, signal, timedOut, aborted, timeoutMs, stdout: { text, truncated }, stderr: {…} }`。
 *
 * @param value - `ToolExecutionSuccess.value`。
 * @returns 规约后的结果；形态不符时返回 undefined。
 */
export function asBashResult(value: unknown): FeedBashResult | undefined {
  const source = asObject(value)
  if (source === undefined) return undefined
  const stdout = asObject(source['stdout'])
  const stderr = asObject(source['stderr'])
  if (stdout === undefined && stderr === undefined) return undefined
  const exitCode = typeof source['exitCode'] === 'number' ? source['exitCode'] : null
  return {
    exitCode,
    stdout: typeof stdout?.['text'] === 'string' ? stdout['text'] : '',
    stderr: typeof stderr?.['text'] === 'string' ? stderr['text'] : '',
    interrupted: source['timedOut'] === true || source['aborted'] === true,
  }
}

const ERROR_MARKERS = [
  'traceback (most recent call last)',
  'error:',
  'exception:',
  'fatal:',
  'command failed',
] as const

/**
 * 粗粒度失败迹象判定，口径与 `extract.py` 的 `_looks_like_error` 一致。
 *
 * @param text - 待判定文本。
 * @returns 前 400 字符内命中任一关键词即为真。
 */
export function looksLikeError(text: string): boolean {
  const head = text.slice(0, 400).toLowerCase()
  return ERROR_MARKERS.some(marker => head.includes(marker))
}

/**
 * 从 bash 结果里取失败信号原文；无失败迹象返回空串。
 *
 * 取值顺序与 `post_tool_use.py` 的 `_bash_error_text` 一致：非空 stderr 最直接，
 * 被中断次之，stdout 命中常见报错关键词兜底。dsh 侧多了 `exitCode`，由调用方先行判定。
 *
 * @param result - 规约后的 bash 结果。
 * @returns 错误原文或空串。
 */
export function bashErrorText(result: FeedBashResult): string {
  if (result.stderr.trim().length > 0) return result.stderr
  if (result.interrupted) return result.stdout.trim().length > 0 ? result.stdout : 'command interrupted'
  if (looksLikeError(result.stdout)) return result.stdout
  return ''
}

/**
 * 把 dsh 的内容块拍平成纯文本，只保留 `type === 'text'` 的块
 * （口径同 `hooks-claude-code/src/index.ts:318-320` 的 `blocksToText`）。
 *
 * @param blocks - 内容块数组。
 * @returns 拼接后的文本。
 */
export function blocksToText(blocks: readonly unknown[]): string {
  const parts: string[] = []
  for (const raw of blocks) {
    const block = asObject(raw)
    if (block === undefined) continue
    if (block['type'] === 'text' && typeof block['text'] === 'string') parts.push(block['text'])
  }
  return parts.join('\n')
}

/**
 * 把会话 id 规约成安全的文件名片段，口径同 `extract.py` 的 `_safe_name`。
 *
 * TS 侧用它命名 feed 与 seen 文件，并把同一个规约后的值作为 `--session` 传给
 * Python，两侧的水位文件因此落在同一个名字上。
 *
 * @param text - 原始会话 id。
 * @returns 安全文件名片段。
 */
export function safeName(text: string): string {
  const replaced = text.replace(/[^A-Za-z0-9._-]+/gu, '_').slice(0, 80)
  return replaced.length > 0 ? replaced : 'unknown'
}
