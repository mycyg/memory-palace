/**
 * `paths.py` 的 `MemoryPaths.relative` 与 `for_project` 的路径规约复刻。
 *
 * 文件锚点的倒排 key 形如 `file:src/foo.py`，由 Python 把绝对路径规约为项目内的
 * POSIX 相对路径后写入。本插件在 `tools/result` 上对同一路径做同样规约才能查中。
 *
 * @module
 */

import { existsSync, realpathSync } from 'node:fs'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'

const realpathCache = new Map<string, string>()
const REALPATH_CACHE_LIMIT = 512

/**
 * 等价于 `PurePosixPath(text).as_posix()`：折叠重复斜杠与 `.` 段，保留 `..`。
 *
 * 与 Python 一致的两处边界：结果为空时得 `.`；恰好两个前导斜杠时保留两个。
 *
 * @param text - 任意路径字符串。
 * @returns 规范化后的 POSIX 路径文本。
 */
export function posixNormalize(text: string): string {
  const source = text.split(sep === '\\' ? /[\\/]/u : '/')
  const parts = source.filter(part => part.length > 0 && part !== '.')
  let prefix = ''
  if (text.startsWith('//') && !text.startsWith('///')) prefix = '//'
  else if (text.startsWith('/')) prefix = '/'
  if (parts.length === 0) return prefix.length > 0 ? prefix : '.'
  return `${prefix}${parts.join('/')}`
}

/**
 * 等价于 Python `Path.resolve()`（非严格模式）：把已存在的最长前缀解到真实路径，
 * 其余部分按字面拼回去。
 *
 * @param input - 任意路径。
 * @returns 绝对路径。
 */
export function pyResolve(input: string): string {
  const absolute = resolve(input)
  const cached = realpathCache.get(absolute)
  if (cached !== undefined) return cached
  const tail: string[] = []
  let current = absolute
  let out = absolute
  for (;;) {
    if (existsSync(current)) {
      try {
        out = join(realpathSync(current), ...tail.slice().reverse())
      } catch {
        out = absolute
      }
      break
    }
    const parent = dirname(current)
    if (parent === current) {
      out = absolute
      break
    }
    tail.push(basename(current))
    current = parent
  }
  if (realpathCache.size >= REALPATH_CACHE_LIMIT) realpathCache.clear()
  realpathCache.set(absolute, out)
  return out
}

/**
 * 把路径规约为项目内的 POSIX 相对路径；项目外的路径原样返回（同 Python 实现）。
 *
 * @param projectDir - 已解析的项目根目录绝对路径。
 * @param path - 待规约的路径，可以是相对或绝对。
 * @returns 倒排 key 里使用的路径文本。
 */
export function relativeToProject(projectDir: string, path: string): string {
  if (!isAbsolute(path)) return posixNormalize(path)
  const resolved = pyResolve(path)
  const rel = relative(projectDir, resolved)
  if (rel === '') return '.'
  const escapes = rel === '..' || rel.startsWith(`..${sep}`) || rel.startsWith('../')
  if (!escapes && !isAbsolute(rel)) return posixNormalize(rel)
  return posixNormalize(path)
}

/**
 * 项目根目录的解析口径，与 `MemoryPaths.for_project` 一致（展开 `~`，再 resolve）。
 *
 * @param projectDir - 会话工作目录。
 * @param home - 用户主目录，用于展开 `~`。
 * @returns 解析后的绝对路径。
 */
export function resolveProjectDir(projectDir: string, home: string): string {
  let text = projectDir
  if (text === '~') text = home
  else if (text.startsWith('~/')) text = join(home, text.slice(2))
  return pyResolve(text)
}
