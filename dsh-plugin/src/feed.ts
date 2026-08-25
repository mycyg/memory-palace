/**
 * feed 落盘：把 dsh 的结构化事件转写成 `extract.py` 认识的 Claude Code 形态 jsonl。
 *
 * `extract.py` 逐行宽容解析，认三种取值：
 * - assistant 记录的 `message.content[].type === 'tool_use'`（取工具名与入参）；
 * - user 记录的 `message.content[].type === 'tool_result'`（取错误文本与 Bash 输出）；
 * - 顶层 `toolUseResult`（Bash 的结构化 stdout/stderr）。
 *
 * 其余形态的行会被安全跳过（既不进 harvest 也不计 skipped_lines），因此 turn/step
 * 边界写成自描述的标记行，只为人读与行号对齐。
 *
 * 同一 feed 文件的写入在进程内串行化：所有追加进同一条 promise 链。
 *
 * @module
 */

import { appendFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'

/** 单条 stdout/stderr 写进 feed 的字符上限。 */
export const MAX_STREAM_CHARS = 2000

/** todo 快照的一条。 */
export interface FeedTodo {
  /** 任务文本。 */
  content: string
  /** 生命周期状态。 */
  status: string
}

/** bash 结果的结构化部分。 */
export interface FeedBashResult {
  /** 退出码；未知时为 null。 */
  exitCode: number | null
  /** 标准输出文本。 */
  stdout: string
  /** 标准错误文本。 */
  stderr: string
  /** 是否被中断（超时或取消）。 */
  interrupted: boolean
}

/**
 * 按码点截断并标注截断事实。
 *
 * @param text - 原始文本。
 * @param limit - 上限字符数。
 * @returns 截断后的文本。
 */
export function clip(text: string, limit = MAX_STREAM_CHARS): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}\n…(truncated)`
}

/**
 * 截断记录里每个字符串字段，非字符串值原样保留。
 *
 * 用于委托工具（Task/Agent 类子 agent 调用，SPEC §3.17）的 `arguments`：里面通常
 * 混着 `subagent_type` 等短字段与可能很长的自由文本（`prompt`/`description`），
 * 只需要防住后者失控，不需要（也不应该）把整个结构降级成一坨字符串——Python 侧
 * 的机械抽取还要按字段名取值。
 *
 * @param value - 待截断的字段集合。
 * @param limit - 单字段上限字符数，默认 {@link MAX_STREAM_CHARS}。
 * @returns 每个字符串字段都不超过 limit 的浅拷贝。
 */
export function clipFields(value: Record<string, unknown>, limit = MAX_STREAM_CHARS): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, raw] of Object.entries(value)) {
    out[key] = typeof raw === 'string' ? clip(raw, limit) : raw
  }
  return out
}

/**
 * assistant 侧的 tool_use 记录。
 *
 * @param callId - 调用标识，用于与 tool_result 配对。
 * @param name - Claude Code 形态的工具名。
 * @param input - 工具入参（只写抽取需要的字段）。
 * @returns 一条 feed 记录。
 */
export function toolUseRecord(callId: string, name: string, input: Record<string, unknown>): unknown {
  return {
    type: 'assistant',
    message: { role: 'assistant', content: [{ type: 'tool_use', id: callId, name, input }] },
  }
}

/**
 * user 侧的 tool_result 记录。
 *
 * @param callId - 与 tool_use 配对的调用标识。
 * @param content - 模型可见的结果文本。
 * @param isError - 该调用是否以错误结束。
 * @param toolUseResult - Bash 的结构化结果，非 Bash 时省略。
 * @returns 一条 feed 记录。
 */
export function toolResultRecord(
  callId: string,
  content: string,
  isError: boolean,
  toolUseResult?: { stdout: string, stderr: string, interrupted: boolean },
): unknown {
  const record: Record<string, unknown> = {
    type: 'user',
    message: {
      role: 'user',
      content: [{ type: 'tool_result', tool_use_id: callId, content, is_error: isError }],
    },
  }
  if (toolUseResult !== undefined) record['toolUseResult'] = toolUseResult
  return record
}

/**
 * turn/step 边界标记。`extract.py` 不认这个 type，会安全跳过。
 *
 * @param kind - 边界名，如 `turn/start`。
 * @param data - 边界携带的计数。
 * @returns 一条 feed 记录。
 */
export function boundaryRecord(kind: string, data: Record<string, unknown>): unknown {
  return { type: `dsh/${kind}`, ...data }
}

/**
 * 一个 feed 文件的串行追加器。
 *
 * 所有 `append` 折进同一条 promise 链，因此同一文件的写入不会交错；`flush()`
 * 等待链上已排队的全部写入落盘。
 */
export class FeedWriter {
  /** feed 文件绝对路径。 */
  readonly path: string
  private chain: Promise<void> = Promise.resolve()
  private failures = 0
  private readonly onError: (message: string) => void

  /**
   * @param path - feed 文件绝对路径。
   * @param onError - 写入失败时的日志回调；失败本身不向调用方抛出。
   */
  constructor(path: string, onError: (message: string) => void = () => { /* 默认静默 */ }) {
    this.path = path
    this.onError = onError
  }

  /**
   * 追加若干条记录。返回的 promise 只用于 flush，调用方可以不等待。
   *
   * @param records - 待写入的记录。
   * @returns 本次写入完成的 promise。
   */
  append(...records: unknown[]): Promise<void> {
    if (records.length === 0) return this.chain
    let payload: string
    try {
      payload = `${records.map(record => JSON.stringify(record)).join('\n')}\n`
    } catch (error) {
      this.failures += 1
      this.onError(`feed 序列化失败 ${error instanceof Error ? error.message : String(error)}`)
      return this.chain
    }
    this.chain = this.chain.then(async () => {
      await mkdir(dirname(this.path), { recursive: true })
      await appendFile(this.path, payload, 'utf8')
    }).catch((error: unknown) => {
      this.failures += 1
      this.onError(`feed 写入失败 ${error instanceof Error ? error.message : String(error)}`)
    })
    return this.chain
  }

  /**
   * 等待已排队的写入全部落盘。
   *
   * @returns 落盘完成的 promise。
   */
  async flush(): Promise<void> {
    await this.chain
  }

  /** 迄今写入失败的次数，供护栏日志与测试观察。 */
  get failureCount(): number {
    return this.failures
  }
}
