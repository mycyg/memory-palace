/**
 * Python 字符串／正则语义的等价实现。
 *
 * eventmem 的锚点 key（`file:` / `error:` / `intent:`）由 Python 侧写入 `.memory/`，
 * 由本插件在热路径上查表。两边对同一输入必须产出同一个 key，否则倒排永远查不中。
 * Python 的 `\w`／`\s`／`\d`／`\b` 在 str 模式下是 Unicode 感知的，JS 的对应写法
 * 默认只认 ASCII；`str.splitlines()`／`str.split()`／切片按码点，JS 按 UTF-16 码元。
 * 本模块把这些差异一次性补齐，其余模块只用这里的原语。
 *
 * @module
 */

/** Python `str.isspace()` 为真的码点集合，写成正则字符类的内容。 */
export const PY_SPACE_CLASS = ' \\t\\n\\v\\f\\r\\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'

/** Python `\w` 的等价字符类内容：字母、数字、下划线（Unicode 全集）。 */
export const PY_WORD_CLASS = '\\p{L}\\p{N}_'

/** Python `\d` 的等价字符类内容：任意十进制数字（不止 ASCII）。 */
export const PY_DIGIT_CLASS = '\\p{Nd}'

/** Python `\b` 在「匹配以词字符开头」时的等价前瞻。 */
export const PY_WORD_BEFORE = `(?<![${PY_WORD_CLASS}])`

/** Python `\b` 在「匹配以词字符结尾」时的等价后瞻。 */
export const PY_WORD_AFTER = `(?![${PY_WORD_CLASS}])`

const SPACE_RUN_RE = new RegExp(`[${PY_SPACE_CLASS}]+`, 'u')
const LEADING_SPACE_RE = new RegExp(`^[${PY_SPACE_CLASS}]+`, 'u')
const TRAILING_SPACE_RE = new RegExp(`[${PY_SPACE_CLASS}]+$`, 'u')
// Python str.splitlines() 的边界：\r\n 优先，其余为单字符换行类。
const LINE_BREAK_RE = new RegExp('\\r\\n|[\\n\\r\\v\\f\\x1c\\x1d\\x1e\\x85\\u2028\\u2029]', 'u')

/** 等价于 Python `str.strip()`（不带参数）。 */
export function pyStrip(text: string): string {
  return text.replace(LEADING_SPACE_RE, '').replace(TRAILING_SPACE_RE, '')
}

/** 等价于 Python `str.lstrip()`（不带参数）。 */
export function pyLStrip(text: string): string {
  return text.replace(LEADING_SPACE_RE, '')
}

/** 等价于 Python `" ".join(text.split())`：按空白切分后以单空格重连，两端不留空白。 */
export function pyCollapseWhitespace(text: string): string {
  return text.split(SPACE_RUN_RE).filter(part => part.length > 0).join(' ')
}

/**
 * 等价于 Python `str.splitlines()`。
 *
 * 与 `split('\n')` 的两处差异都在这里补上：换行边界是一个字符集合而非单个 `\n`；
 * 空串得到空数组，末尾的换行不产生尾随空串。
 */
export function pySplitLines(text: string): string[] {
  if (text.length === 0) return []
  const parts = text.split(new RegExp(LINE_BREAK_RE.source, 'gu'))
  if (parts.length > 0 && parts[parts.length - 1] === '') parts.pop()
  return parts
}

/** 按码点数取长度，等价于 Python 的 `len(str)`。 */
export function codePointLength(text: string): number {
  let count = 0
  for (const _ of text) count += 1
  return count
}

/** 按码点切片，等价于 Python 的 `text[:limit]`。 */
export function codePointSlice(text: string, limit: number): string {
  if (limit <= 0) return ''
  let count = 0
  let end = 0
  for (const char of text) {
    if (count >= limit) break
    end += char.length
    count += 1
  }
  return text.slice(0, end)
}
