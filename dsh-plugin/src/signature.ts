/**
 * `recall.py` 的 `error_signature` 与 `_error_line` 的逐规则复刻。
 *
 * 规范化顺序不可换（时间戳 → POSIX 路径 → Windows 路径 → 行号 → 十六进制地址），
 * 后者会吃掉前者的数字。产出必须与 Python 侧逐字节一致：错误倒排的 key 由 Python
 * 写入 anchors.json，由本插件查表。
 *
 * @module
 */

import {
  PY_DIGIT_CLASS,
  PY_SPACE_CLASS,
  PY_WORD_AFTER,
  PY_WORD_BEFORE,
  PY_WORD_CLASS,
  codePointSlice,
  pyCollapseWhitespace,
  pyLStrip,
  pySplitLines,
  pyStrip,
} from './pycompat.js'

/** 错误签名的截断长度，与 recall.py 的 SIGNATURE_CHARS 一致。 */
export const SIGNATURE_CHARS = 120

const TRACEBACK_HEAD = 'Traceback (most recent call last)'

const D = `[${PY_DIGIT_CLASS}]`

const TS_FULL_RE = new RegExp(
  `${D}{4}-${D}{2}-${D}{2}[T ]${D}{2}:${D}{2}:${D}{2}(?:\\.${D}+)?(?:Z|[+-]${D}{2}:?${D}{2})?`,
  'gu',
)
const TS_CLOCK_RE = new RegExp(
  `${PY_WORD_BEFORE}${D}{2}:${D}{2}:${D}{2}(?:\\.${D}+)?${PY_WORD_AFTER}`,
  'gu',
)
const POSIX_PATH_RE = new RegExp(
  `(?<![${PY_WORD_CLASS}:/])(?:/[^/${PY_SPACE_CLASS}'"\`:,;()\\[\\]]+)+`,
  'gu',
)
const WIN_PATH_RE = new RegExp(
  `${PY_WORD_BEFORE}[A-Za-z]:\\\\(?:[^\\\\${PY_SPACE_CLASS}'"\`,;()\\[\\]]+\\\\?)+`,
  'gu',
)
const LINENO_WORD_RE = new RegExp(`${PY_WORD_BEFORE}line ${D}+`, 'giu')
const LINENO_COLON_RE = new RegExp(`(\\.[A-Za-z]{1,5}):${D}+(?::${D}+)?`, 'gu')
const HEX_ADDR_RE = new RegExp(`${PY_WORD_BEFORE}0[xX][0-9a-fA-F]+`, 'gu')

/**
 * 错误签名规范化：取错误行，去绝对路径、行号、地址、时间戳，压空白后截 120 字符。
 *
 * 规范化幂等——已是签名的入参不会被二次改写。
 *
 * @param stderr - 原始错误输出，可为多行。
 * @returns 规范化签名；无可用错误行时返回空串。
 */
export function errorSignature(stderr: string): string {
  let line = errorLine(stderr)
  if (line.length === 0) return ''
  line = line.replace(TS_FULL_RE, '<TS>')
  line = line.replace(TS_CLOCK_RE, '<TS>')
  line = line.replace(POSIX_PATH_RE, match => match.slice(match.lastIndexOf('/') + 1))
  line = line.replace(WIN_PATH_RE, (match) => {
    const trimmed = match.replace(/\\+$/u, '')
    return trimmed.slice(trimmed.lastIndexOf('\\') + 1)
  })
  line = line.replace(LINENO_WORD_RE, 'line N')
  line = line.replace(LINENO_COLON_RE, '$1:N')
  line = line.replace(HEX_ADDR_RE, '<ADDR>')
  line = pyCollapseWhitespace(line)
  return codePointSlice(line, SIGNATURE_CHARS)
}

/**
 * 取用于签名的那一行：Python traceback 取末行异常，其余取首个非空行。
 *
 * @param stderr - 原始错误输出。
 * @returns 该行去除两端空白后的内容；全为空白时返回空串。
 */
export function errorLine(stderr: string): string {
  const lines = pySplitLines(stderr).filter(line => pyStrip(line).length > 0)
  if (lines.length === 0) return ''
  if (lines.some(line => pyLStrip(line).startsWith(TRACEBACK_HEAD))) {
    return pyStrip(lines[lines.length - 1] ?? '')
  }
  return pyStrip(lines[0] ?? '')
}
