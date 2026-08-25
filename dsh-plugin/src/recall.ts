/**
 * 浮现（surface）：`recall.py` 中不含 BM25 的那一半，纯查表，毫秒级，不调模型。
 *
 * 流程与 Python 逐条对应：线索 → 倒排 key → anchors.json 命中 → 过滤 seen →
 * 状态权重与新近度排序 → 截 K → 单行渲染。
 *
 * @module
 */

import { readFileSync, statSync } from 'node:fs'

import { readEventHead } from './eventfile.js'
import type { MemoryPaths } from './memory.js'
import { codePointLength, codePointSlice, pyCollapseWhitespace, pyStrip } from './pycompat.js'
import { errorSignature } from './signature.js'
import { anchorKey, intentTokens } from './tokenize.js'

/** 浮现与检索单行的截断长度，与 recall.py 的 LINE_CHARS 一致。 */
export const LINE_CHARS = 120

/** 线索类型。 */
export type CueKind = 'file' | 'error' | 'intent'

/** 状态权重：已闭合的经验价值高于进行中的，被推翻的最低（DESIGN §2.4）。 */
const STATUS_WEIGHT: Readonly<Record<string, number>> = Object.freeze({
  done: 3,
  abandoned: 2,
  open: 1,
  superseded: 0,
})

/** 一条浮现结果：事件 id ＋ 注入用的单行。 */
export interface SurfaceHit {
  /** 命中的事件 id。 */
  eventId: string
  /** 注入用的单行文本，形如 `[id] outcome`。 */
  line: string
}

interface AnchorCache {
  mtimeMs: number
  size: number
  map: Map<string, string[]>
}

let anchorCache: { path: string, entry: AnchorCache } | undefined

/**
 * 读锚点倒排；文件缺失或损坏返回空表。带 mtime/size 失效的进程内缓存，Python 侧
 * 重建索引后（原子替换，mtime 变化）自动失效。
 *
 * @param paths - 记忆路径视图。
 * @returns key 到事件 id 列表的映射。
 */
export function loadAnchorMap(paths: MemoryPaths): Map<string, string[]> {
  const path = paths.anchors
  let stat
  try {
    stat = statSync(path)
  } catch {
    anchorCache = undefined
    return new Map()
  }
  const cached = anchorCache
  if (
    cached !== undefined
    && cached.path === path
    && cached.entry.mtimeMs === stat.mtimeMs
    && cached.entry.size === stat.size
  ) {
    return cached.entry.map
  }
  const map = new Map<string, string[]>()
  try {
    const loaded: unknown = JSON.parse(readFileSync(path, 'utf8'))
    if (loaded !== null && typeof loaded === 'object' && !Array.isArray(loaded)) {
      for (const [key, ids] of Object.entries(loaded as Record<string, unknown>)) {
        if (Array.isArray(ids)) map.set(String(key), ids.map(id => String(id)))
      }
    }
  } catch {
    map.clear()
  }
  anchorCache = { path, entry: { mtimeMs: stat.mtimeMs, size: stat.size, map } }
  return map
}

/** 清空锚点缓存（测试用）。 */
export function clearAnchorCache(): void {
  anchorCache = undefined
}

/**
 * 把线索转成倒排 key；intent 线索按词元展开成多个 key。
 *
 * @param cue - 线索原文。
 * @param kind - 线索类型。
 * @param paths - 记忆路径视图，file 线索需要它做路径规约。
 * @returns 倒排 key 列表；线索为空时返回空数组。
 */
export function cueKeys(cue: string, kind: CueKind, paths: MemoryPaths): string[] {
  const text = pyStrip(cue)
  if (text.length === 0) return []
  if (kind === 'file') return [anchorKey('file', paths.relative(text))]
  if (kind === 'error') {
    const signature = errorSignature(text)
    return signature.length > 0 ? [anchorKey('error', signature)] : []
  }
  return intentTokens(text).map(token => anchorKey('intent', token))
}

/**
 * 线索浮现：锚点精确命中 → 过滤 seen → 按状态权重与新近度排序 → 截 K。
 *
 * @param cue - 线索原文。
 * @param kind - 线索类型。
 * @param paths - 记忆路径视图。
 * @param surfaceK - 单次浮现的条数上限。
 * @param seen - 本会话已浮现过的事件 id。
 * @returns 浮现结果，最多 `surfaceK` 条。
 */
export function surface(
  cue: string,
  kind: CueKind,
  paths: MemoryPaths,
  surfaceK: number,
  seen: ReadonlySet<string>,
): SurfaceHit[] {
  const keys = cueKeys(cue, kind, paths)
  if (keys.length === 0) return []
  const anchors = loadAnchorMap(paths)

  const overlap = new Map<string, number>()
  for (const key of keys) {
    for (const eventId of anchors.get(key) ?? []) {
      if (seen.has(eventId)) continue
      overlap.set(eventId, (overlap.get(eventId) ?? 0) + 1)
    }
  }
  if (overlap.size === 0) return []

  const ranked: { id: string, weight: number, hits: number, line: string }[] = []
  for (const [eventId, hits] of overlap) {
    const head = readEventHead(paths.eventFile(eventId))
    if (head === undefined) continue // 索引比存储旧时跳过，宁漏勿胀
    ranked.push({
      id: head.id,
      weight: STATUS_WEIGHT[head.status] ?? 0,
      hits,
      line: hitLine(head.id, head.outcome, head.intent),
    })
  }

  // 状态权重优先，其次新近度（时间戳 id 的字典序即时间序），命中词元数只做末位打破平局
  ranked.sort((a, b) => {
    if (a.weight !== b.weight) return b.weight - a.weight
    if (a.id !== b.id) return a.id < b.id ? 1 : -1
    return b.hits - a.hits
  })
  return ranked.slice(0, surfaceK).map(item => ({ eventId: item.id, line: item.line }))
}

/**
 * 单行召回格式：有 outcome 用 outcome，否则退回 intent。
 *
 * @param eventId - 事件 id。
 * @param outcome - 事件结论，可为空。
 * @param intent - 事件意图。
 * @returns 形如 `[id] 结论` 的单行。
 */
export function hitLine(eventId: string, outcome: string | undefined, intent: string): string {
  const text = outcome !== undefined && outcome !== '' ? outcome : intent
  let flat = pyCollapseWhitespace(text)
  if (codePointLength(flat) > LINE_CHARS) flat = `${codePointSlice(flat, LINE_CHARS - 1)}…`
  return `[${eventId}] ${flat}`
}
