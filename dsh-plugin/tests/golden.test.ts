/**
 * 黄金 fixture 对照：错误签名、词元化、文件 key 三处规约必须与 Python 逐字节一致。
 *
 * fixture 由 `scripts/gen-fixtures.py` 用 `.venv/bin/python` 直接调 Python 函数生成。
 * 这是两实现互操作的生命线——任何一侧漂移都会让倒排索引查不中。
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { beforeAll, describe, expect, it } from 'vitest'

import { MemoryPaths } from '../src/memory.js'
import { errorSignature } from '../src/signature.js'
import { intentTokens, tokenize } from '../src/tokenize.js'
import { fixture } from './helpers.js'

interface SignatureCase { input: string, output: string }
interface TokenCase { input: string, tokens: string[], intentTokens: string[] }
interface FileKeyFixture {
  projectDir: string
  resolvedProjectDir: string
  cases: { input: string, output: string }[]
}

const signatures = fixture<SignatureCase[]>('signatures.json')
const tokens = fixture<TokenCase[]>('tokens.json')
const fileKeys = fixture<FileKeyFixture>('file-keys.json')

describe('errorSignature', () => {
  it('fixture 覆盖中文、traceback、路径与十六进制', () => {
    expect(signatures.length).toBeGreaterThanOrEqual(20)
  })

  it.each(signatures.map((item, index) => [index, item] as const))(
    '#%i 与 Python 输出逐字节一致',
    (_index, item) => {
      expect(errorSignature(item.input)).toBe(item.output)
    },
  )

  it('对已是签名的输入幂等', () => {
    for (const item of signatures) {
      expect(errorSignature(item.output)).toBe(errorSignature(errorSignature(item.output)))
    }
  })
})

describe('tokenize / intentTokens', () => {
  it.each(tokens.map((item, index) => [index, item] as const))(
    '#%i 与 Python 输出逐字节一致',
    (_index, item) => {
      expect(tokenize(item.input)).toEqual(item.tokens)
      expect(intentTokens(item.input)).toEqual(item.intentTokens)
    },
  )
})

describe('文件 key 规约', () => {
  beforeAll(() => {
    // fixture 生成时项目目录树是真实存在的（Path.resolve() 要解符号链接），
    // 断言前必须复现同一棵树，否则 macOS 的 /tmp -> /private/tmp 解不出来。
    const root = fileKeys.projectDir
    mkdirSync(join(root, 'src'), { recursive: true })
    mkdirSync(join(root, '中文目录'), { recursive: true })
    mkdirSync(join(root, '..', 'other'), { recursive: true })
    writeFileSync(join(root, 'src', 'foo.py'), '# fixture\n', 'utf8')
    writeFileSync(join(root, '中文目录', '文件.md'), '# fixture\n', 'utf8')
    writeFileSync(join(root, '..', 'other', 'baz.py'), '# fixture\n', 'utf8')
  })

  it('项目根解析口径与 Python 一致', () => {
    expect(MemoryPaths.forProject(fileKeys.projectDir).projectDir).toBe(fileKeys.resolvedProjectDir)
  })

  it.each(fileKeys.cases.map((item, index) => [index, item] as const))(
    '#%i 与 Python 输出逐字节一致',
    (_index, item) => {
      expect(MemoryPaths.forProject(fileKeys.projectDir).relative(item.input)).toBe(item.output)
    },
  )
})
