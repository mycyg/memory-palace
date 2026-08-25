/**
 * `index.py` 的 `tokenize` / `intent_tokens` / `anchor_key` 的逐规则复刻。
 *
 * 产出必须与 Python 侧逐字节一致：intent 倒排的 key 由 Python 写入 anchors.json，
 * 由本插件查表。测试用黄金 fixture（`tests/fixtures/tokens.json`）对照断言。
 *
 * @module
 */

import { codePointLength } from './pycompat.js'

// 与 index.py 的 _TOKEN_RE 同源：拉丁词元与 CJK 连续段（CJK 段随后切成字符 bigram）。
const TOKEN_RE = new RegExp('[a-z0-9]+|[\\u3400-\\u4dbf\\u4e00-\\u9fff\\u3040-\\u30ff]+', 'gu')

/**
 * 分词：按非字母数字切出拉丁词元，中文按字符 bigram。
 *
 * @param text - 原始文本，内部先做小写化。
 * @returns 词元列表，顺序与 Python 实现一致。
 */
export function tokenize(text: string): string[] {
  const tokens: string[] = []
  for (const match of text.toLowerCase().matchAll(TOKEN_RE)) {
    const chunk = match[0]
    const first = chunk.codePointAt(0)
    if (first !== undefined && first < 128) {
      tokens.push(chunk)
      continue
    }
    const chars = Array.from(chunk)
    if (chars.length === 1) {
      tokens.push(chunk)
      continue
    }
    for (let i = 0; i < chars.length - 1; i += 1) {
      tokens.push(`${chars[i] ?? ''}${chars[i + 1] ?? ''}`)
    }
  }
  return tokens
}

/**
 * intent 倒排用的词元：长度 ≥2，去重保序。
 *
 * @param text - intent 文本或 todo 文本。
 * @returns 去重后的词元列表。
 */
export function intentTokens(text: string): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const token of tokenize(text)) {
    if (codePointLength(token) >= 2 && !seen.has(token)) {
      seen.add(token)
      out.push(token)
    }
  }
  return out
}

/**
 * 拼倒排索引的 key。
 *
 * @param kind - `file` / `error` / `intent` 三类之一。
 * @param cue - 已规约的线索值。
 * @returns 形如 `file:src/foo.py` 的 key。
 */
export function anchorKey(kind: string, cue: string): string {
  return `${kind}:${cue}`
}
