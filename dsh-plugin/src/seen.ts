/**
 * 同会话浮现去重集合。落盘格式与 Python 侧 `hooks/__init__.py` 的
 * `load_seen` / `append_seen` 一致：每行一个事件 id，UTF-8，追加写。
 *
 * 双宿主共享同一个文件：Claude Code 的 hook 与本插件在同一项目下用同一份 seen，
 * 因此这里是 B-lite 边界上允许 TS 写入的两类文件之一。
 *
 * @module
 */

import { appendFileSync, mkdirSync, readFileSync } from 'node:fs'
import { dirname } from 'node:path'

import type { MemoryPaths } from './memory.js'

/**
 * 读取本会话已浮现过的事件 id；文件不存在或不可读返回空集合。
 *
 * @param paths - 记忆路径视图。
 * @param sessionId - 会话 id。
 * @returns 事件 id 集合。
 */
export function loadSeen(paths: MemoryPaths, sessionId: string): Set<string> {
  try {
    const text = readFileSync(paths.seenFile(sessionId), 'utf8')
    const out = new Set<string>()
    for (const line of text.split('\n')) {
      const trimmed = line.trim()
      if (trimmed.length > 0) out.add(trimmed)
    }
    return out
  } catch {
    return new Set()
  }
}

/**
 * 追加新命中的事件 id，每行一个；写入失败静默（不影响本次浮现结果）。
 *
 * @param paths - 记忆路径视图。
 * @param sessionId - 会话 id。
 * @param eventIds - 待追加的事件 id。
 */
export function appendSeen(paths: MemoryPaths, sessionId: string, eventIds: readonly string[]): void {
  const ids = eventIds.filter(id => id.length > 0)
  if (ids.length === 0) return
  try {
    const file = paths.seenFile(sessionId)
    mkdirSync(dirname(file), { recursive: true })
    appendFileSync(file, `${ids.join('\n')}\n`, 'utf8')
  } catch {
    // seen 写入失败不影响浮现
  }
}
