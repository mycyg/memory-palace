/**
 * L0 事件文件（yaml frontmatter ＋ 正文）的最小只读解析。
 *
 * 浮现只需要 `id` / `status` / `intent` / `outcome` 四个顶层标量字段，因此这里不引
 * yaml 库，只按行取顶层键，并支持 `schema.py` 的自定义 representer 会产出的两种形态：
 * 普通标量与 `|` 块标量。解析失败按「事件不可用」处理并跳过，口径同 Python 侧
 * `surface` 对 `SchemaError` 的处理（宁漏勿胀）。
 *
 * @module
 */

import { readFileSync, statSync } from 'node:fs'

/** 状态枚举，与 `schema.py` 的 STATUSES 一致。 */
export const STATUSES = ['open', 'done', 'abandoned', 'superseded'] as const

/** 浮现需要的事件字段。 */
export interface EventHead {
  /** 事件 id。 */
  id: string
  /** 事件状态。 */
  status: string
  /** 意图，一句话。 */
  intent: string
  /** 结论；未闭合时为 undefined。 */
  outcome: string | undefined
}

interface CacheEntry {
  mtimeMs: number
  size: number
  head: EventHead | undefined
}

const cache = new Map<string, CacheEntry>()
const CACHE_LIMIT = 1024

const FENCE = '---'
const TOP_KEY_RE = /^([A-Za-z_][A-Za-z0-9_]*):(.*)$/u

/**
 * 读取一个事件文件的浮现字段，带 mtime/size 失效的进程内缓存。
 *
 * @param path - 事件文件绝对路径。
 * @returns 事件字段；文件缺失或不合 schema 时返回 undefined。
 */
export function readEventHead(path: string): EventHead | undefined {
  let stat
  try {
    stat = statSync(path)
  } catch {
    cache.delete(path)
    return undefined
  }
  const hit = cache.get(path)
  if (hit !== undefined && hit.mtimeMs === stat.mtimeMs && hit.size === stat.size) return hit.head
  let head: EventHead | undefined
  try {
    head = parseEventHead(readFileSync(path, 'utf8'))
  } catch {
    head = undefined
  }
  if (cache.size >= CACHE_LIMIT) cache.clear()
  cache.set(path, { mtimeMs: stat.mtimeMs, size: stat.size, head })
  return head
}

/** 清空事件缓存（测试用）。 */
export function clearEventCache(): void {
  cache.clear()
}

/**
 * 解析事件文本的 frontmatter。
 *
 * @param text - 事件文件全文。
 * @returns 事件字段；缺必填字段或状态非法时返回 undefined。
 */
export function parseEventHead(text: string): EventHead | undefined {
  const front = parseFrontmatter(text)
  if (front === undefined) return undefined
  const id = front.get('id')
  const status = front.get('status')
  const intent = front.get('intent')
  const kind = front.get('kind')
  if (id === undefined || status === undefined || intent === undefined || kind === undefined) return undefined
  if (id.trim() === '' || status.trim() === '' || intent.trim() === '' || kind.trim() === '') return undefined
  if (!(STATUSES as readonly string[]).includes(status)) return undefined
  const outcome = front.get('outcome')
  return { id, status, intent, outcome: outcome === undefined || outcome === '' ? undefined : outcome }
}

/**
 * 取 frontmatter 里的顶层标量键值对；嵌套映射（`anchors`）整体跳过。
 *
 * @param text - 事件文件全文。
 * @returns 键到字符串值的映射；`null` 值不进入映射。
 */
function parseFrontmatter(text: string): Map<string, string> | undefined {
  const normalized = text.replace(/^﻿/u, '')
  if (!normalized.startsWith(FENCE)) return undefined
  const lines = normalized.split('\n')
  if (lines[0]?.trim() !== FENCE) return undefined
  const out = new Map<string, string>()
  let index = 1
  let closed = false
  while (index < lines.length) {
    const line = lines[index] ?? ''
    if (line.trim() === FENCE && !line.startsWith(' ')) {
      closed = true
      break
    }
    const match = TOP_KEY_RE.exec(line)
    if (match === null) {
      index += 1
      continue
    }
    const key = match[1] ?? ''
    const raw = (match[2] ?? '').replace(/^ /u, '')
    if (raw.startsWith('|') || raw.startsWith('>')) {
      const block = readBlockScalar(lines, index + 1, raw)
      out.set(key, block.value)
      index = block.next
      continue
    }
    const scalar = parseScalar(raw)
    if (scalar !== undefined) out.set(key, scalar)
    index += 1
  }
  return closed ? out : undefined
}

/**
 * 解析单行标量：`null` / `~` / 空 得 undefined，引号形式剥引号。
 *
 * @param raw - 冒号之后的原始文本。
 * @returns 字符串值，或表示「无值」的 undefined。
 */
function parseScalar(raw: string): string | undefined {
  const value = raw.replace(/\s+$/u, '')
  if (value === '' || value === 'null' || value === '~' || value === 'Null' || value === 'NULL') return undefined
  if (value.startsWith("'") && value.endsWith("'") && value.length >= 2) {
    return value.slice(1, -1).replaceAll("''", "'")
  }
  if (value.startsWith('"') && value.endsWith('"') && value.length >= 2) {
    try {
      const parsed: unknown = JSON.parse(value)
      if (typeof parsed === 'string') return parsed
    } catch {
      // 回退到朴素剥引号
    }
    return value.slice(1, -1)
  }
  return value
}

/**
 * 解析 `|` 块标量：收集后续更深缩进的行，去掉块缩进，按 chomping 指示符收尾。
 *
 * @param lines - 全文按行切分的结果。
 * @param start - 块内容的首行下标。
 * @param header - 冒号之后的原始文本，形如 `|`、`|-`、`|2+`。
 * @returns 块文本与下一个待处理行下标。
 */
function readBlockScalar(lines: string[], start: number, header: string): { value: string, next: number } {
  const chomp = header.includes('-') ? 'strip' : header.includes('+') ? 'keep' : 'clip'
  const explicit = /[1-9]/u.exec(header)
  const body: string[] = []
  let indent = explicit === null ? 0 : Number(explicit[0])
  let index = start
  while (index < lines.length) {
    const line = lines[index] ?? ''
    if (line.trim() === '') {
      body.push('')
      index += 1
      continue
    }
    const lead = line.length - line.replace(/^ +/u, '').length
    if (indent === 0) {
      if (lead === 0) break
      indent = lead
    }
    if (lead < indent) break
    body.push(line.slice(indent))
    index += 1
  }
  while (body.length > 0 && body[body.length - 1] === '') body.pop()
  let value = body.join('\n')
  if (chomp !== 'strip' && value.length > 0) value += '\n'
  return { value, next: index }
}
