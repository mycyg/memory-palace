/**
 * 护栏日志与「绝不向宿主抛异常」的包装器。
 *
 * 纪律与 Python hooks 同级：一切监听器的异常都在这里被吞掉并落到
 * `.memory/log/eventmem-dsh.log`；写日志本身失败也静默，否则失去了护栏的意义。
 *
 * @module
 */

import { appendFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

import type { MemoryPaths } from './memory.js'

/**
 * 日志落点的解析方式：直接给路径视图，或给一个只在出错时才被调用的解析函数。
 *
 * 用函数形态是为了让热路径上的监听器不必为「可能不会发生的异常」预先解析路径。
 */
export type LogTarget = MemoryPaths | (() => MemoryPaths | undefined) | undefined

/**
 * 解析日志落点，解析本身失败也不抛。
 *
 * @param target - 路径视图或解析函数。
 * @returns 路径视图，或 undefined。
 */
function resolveTarget(target: LogTarget): MemoryPaths | undefined {
  if (target === undefined) return undefined
  if (typeof target !== 'function') return target
  try {
    return target()
  } catch {
    return undefined
  }
}

/**
 * 写一行护栏日志，尽力而为。
 *
 * @param target - 日志落点；为 undefined 时静默丢弃。
 * @param message - 日志正文。
 */
export function guardLog(target: LogTarget, message: string): void {
  const paths = resolveTarget(target)
  if (paths === undefined) return
  try {
    const file = paths.adapterLog
    mkdirSync(dirname(file), { recursive: true })
    appendFileSync(file, `${new Date().toISOString()} ${message}\n`, 'utf8')
  } catch {
    // 日志失败静默
  }
}

/**
 * 把任意抛出物转成可读的一行。
 *
 * @param error - 捕获到的值。
 * @returns 描述文本。
 */
export function describe(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  return String(error)
}

/**
 * 同步监听器护栏：异常写日志，绝不外泄。
 *
 * @param target - 日志落点，出错时才解析。
 * @param label - 出错时写进日志的标签。
 * @param body - 被保护的同步逻辑。
 */
export function guard(target: LogTarget, label: string, body: () => void): void {
  try {
    body()
  } catch (error) {
    guardLog(target, `${label} 异常 ${describe(error)}`)
  }
}

/**
 * 异步监听器护栏：异常写日志，绝不外泄。
 *
 * @param target - 日志落点，出错时才解析。
 * @param label - 出错时写进日志的标签。
 * @param body - 被保护的异步逻辑。
 */
export async function guardAsync(
  target: LogTarget,
  label: string,
  body: () => Promise<void>,
): Promise<void> {
  try {
    await body()
  } catch (error) {
    guardLog(target, `${label} 异常 ${describe(error)}`)
  }
}
