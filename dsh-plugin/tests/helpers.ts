/**
 * 测试公共件：fixture 读取、临时项目目录、Python 解释器定位。
 *
 * @module
 */

import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** 本测试目录的绝对路径。 */
export const TESTS_DIR = dirname(fileURLToPath(import.meta.url))

/** 包根目录。 */
export const PACKAGE_DIR = resolve(TESTS_DIR, '..')

/** Python 侧 eventmem 仓库根（`event-memory/`）。 */
export const EVENTMEM_DIR = resolve(PACKAGE_DIR, '..')

/**
 * 读取一个 fixture 文件。
 *
 * @param name - `tests/fixtures/` 下的文件名。
 * @returns 解析后的 JSON。
 */
export function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(TESTS_DIR, 'fixtures', name), 'utf8')) as T
}

/**
 * 定位可用的 Python 解释器：优先仓库自带的 `.venv`。
 *
 * @returns 解释器路径；没有可用解释器时返回 undefined。
 */
export function pythonExecutable(): string | undefined {
  const venv = join(EVENTMEM_DIR, '.venv', 'bin', 'python')
  return existsSync(venv) ? venv : undefined
}

/**
 * 建一个带 `.memory/` 骨架的临时项目目录。
 *
 * @param prefix - 目录名前缀。
 * @returns 项目根目录绝对路径。
 */
export function makeProject(prefix = 'eventmem-dsh-'): string {
  const root = mkdtempSync(join(tmpdir(), prefix))
  for (const sub of ['events', 'index', 'log', 'raw']) {
    mkdirSync(join(root, '.memory', sub), { recursive: true })
  }
  return root
}

/** 写一个 L0 事件文件所需的字段。 */
export interface EventInput {
  /** 事件 id。 */
  id: string
  /** 事件状态。 */
  status: string
  /** 意图。 */
  intent: string
  /** 结论，可省略。 */
  outcome?: string
  /** 种类，默认 build。 */
  kind?: string
}

/**
 * 写一个与 `schema.py` 同形态的 L0 事件文件。
 *
 * @param projectDir - 项目根目录。
 * @param event - 事件字段。
 */
export function writeEvent(projectDir: string, event: EventInput): void {
  const front = [
    '---',
    `id: ${event.id}`,
    'parent: null',
    `kind: ${event.kind ?? 'build'}`,
    `status: ${event.status}`,
    'superseded_by: null',
    `intent: ${event.intent}`,
    'anchors:',
    '  commits: []',
    '  files: []',
    '  tests: []',
    '  dialog: []',
    '  error_sigs: []',
    `outcome: ${event.outcome === undefined ? 'null' : event.outcome}`,
    'lesson: null',
    '---',
    '',
  ].join('\n')
  writeFileSync(join(projectDir, '.memory', 'events', `${event.id}.md`), front, 'utf8')
}

/**
 * 写锚点倒排索引。
 *
 * @param projectDir - 项目根目录。
 * @param anchors - key 到事件 id 列表的映射。
 */
export function writeAnchors(projectDir: string, anchors: Record<string, string[]>): void {
  writeFileSync(
    join(projectDir, '.memory', 'index', 'anchors.json'),
    `${JSON.stringify(anchors, null, 2)}\n`,
    'utf8',
  )
}

/**
 * 读一个文本文件，缺失时返回空串。
 *
 * @param path - 文件绝对路径。
 * @returns 文件内容。
 */
export function readIfExists(path: string): string {
  try {
    return readFileSync(path, 'utf8')
  } catch {
    return ''
  }
}
