/**
 * 浮现：排序、K 截断、seen 去重、索引比存储旧时的降级。
 *
 * 排序口径同 `recall.py`：状态权重（done > abandoned > open > superseded）优先，
 * 其次新近度（时间戳 id 的字典序即时间序），命中词元数只做末位打破平局。
 */

import { rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { clearEventCache, parseEventHead } from '../src/eventfile.js'
import { MemoryPaths } from '../src/memory.js'
import { clearAnchorCache, hitLine, surface } from '../src/recall.js'
import { anchorKey, intentTokens } from '../src/tokenize.js'
import { makeProject, writeAnchors, writeEvent } from './helpers.js'

let project: string
let paths: MemoryPaths

beforeEach(() => {
  project = makeProject()
  paths = MemoryPaths.forProject(project)
  clearAnchorCache()
  clearEventCache()
})

afterEach(() => {
  rmSync(project, { recursive: true, force: true })
})

describe('surface', () => {
  it('按状态权重排序，同权重时新的在前', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'open', intent: 'A', outcome: 'a' })
    writeEvent(project, { id: '2026-08-02_090000', status: 'done', intent: 'B', outcome: 'b' })
    writeEvent(project, { id: '2026-08-03_090000', status: 'done', intent: 'C', outcome: 'c' })
    writeEvent(project, { id: '2026-08-04_090000', status: 'superseded', intent: 'D', outcome: 'd' })
    writeAnchors(project, {
      'file:src/foo.py': [
        '2026-08-01_090000',
        '2026-08-02_090000',
        '2026-08-03_090000',
        '2026-08-04_090000',
      ],
    })

    const hits = surface('src/foo.py', 'file', paths, 10, new Set())
    expect(hits.map(hit => hit.eventId)).toEqual([
      '2026-08-03_090000', // done，较新
      '2026-08-02_090000', // done，较旧
      '2026-08-01_090000', // open
      '2026-08-04_090000', // superseded
    ])
  })

  it('截到 K 条', () => {
    for (let i = 1; i <= 6; i += 1) {
      const id = `2026-08-0${String(i)}_090000`
      writeEvent(project, { id, status: 'done', intent: `意图 ${String(i)}`, outcome: `结论 ${String(i)}` })
    }
    writeAnchors(project, {
      'file:src/foo.py': [1, 2, 3, 4, 5, 6].map(i => `2026-08-0${String(i)}_090000`),
    })
    expect(surface('src/foo.py', 'file', paths, 3, new Set())).toHaveLength(3)
    expect(surface('src/foo.py', 'file', paths, 1, new Set())).toHaveLength(1)
  })

  it('过滤 seen 里的事件', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeEvent(project, { id: '2026-08-02_090000', status: 'done', intent: 'B', outcome: 'b' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000', '2026-08-02_090000'] })

    const hits = surface('src/foo.py', 'file', paths, 10, new Set(['2026-08-02_090000']))
    expect(hits.map(hit => hit.eventId)).toEqual(['2026-08-01_090000'])
  })

  it('索引里有但事件文件缺失时跳过，不报错', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000', '2026-08-09_999999'] })
    expect(surface('src/foo.py', 'file', paths, 10, new Set()).map(hit => hit.eventId))
      .toEqual(['2026-08-01_090000'])
  })

  it('事件文件损坏时跳过', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeFileSync(join(project, '.memory', 'events', '2026-08-02_090000.md'), 'not frontmatter\n', 'utf8')
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000', '2026-08-02_090000'] })
    expect(surface('src/foo.py', 'file', paths, 10, new Set())).toHaveLength(1)
  })

  it('绝对路径线索先被规约成项目内相对路径再查表', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeAnchors(project, { 'file:src/foo.py': ['2026-08-01_090000'] })
    expect(surface(join(project, 'src/foo.py'), 'file', paths, 3, new Set())).toHaveLength(1)
  })

  it('error 线索先规范化成签名再查表', () => {
    const signatureKey = anchorKey('error', 'ValueError: port busy')
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: '改端口区间' })
    writeAnchors(project, { [signatureKey]: ['2026-08-01_090000'] })

    const stderr = 'Traceback (most recent call last):\n  File "/x/y.py", line 3\nValueError: port busy'
    expect(surface(stderr, 'error', paths, 3, new Set()).map(hit => hit.line))
      .toEqual(['[2026-08-01_090000] 改端口区间'])
  })

  it('intent 线索按词元展开，命中多个词元的事件靠状态与新近度定序', () => {
    writeEvent(project, { id: '2026-08-01_090000', status: 'done', intent: 'A', outcome: 'a' })
    writeEvent(project, { id: '2026-08-02_090000', status: 'done', intent: 'B', outcome: 'b' })
    const anchors: Record<string, string[]> = {}
    for (const token of intentTokens('修复 Ray 端口冲突')) anchors[anchorKey('intent', token)] = ['2026-08-01_090000']
    anchors[anchorKey('intent', 'ray')] = ['2026-08-01_090000', '2026-08-02_090000']
    writeAnchors(project, anchors)

    expect(surface('修复 Ray 端口冲突', 'intent', paths, 10, new Set()).map(hit => hit.eventId))
      .toEqual(['2026-08-02_090000', '2026-08-01_090000'])
  })

  it('空线索与无索引都返回空', () => {
    expect(surface('   ', 'file', paths, 3, new Set())).toEqual([])
    expect(surface('src/foo.py', 'file', paths, 3, new Set())).toEqual([])
  })

  it('anchors.json 损坏时返回空而不是抛异常', () => {
    writeFileSync(join(project, '.memory', 'index', 'anchors.json'), '{ not json', 'utf8')
    expect(surface('src/foo.py', 'file', paths, 3, new Set())).toEqual([])
  })
})

describe('hitLine', () => {
  it('有 outcome 用 outcome，否则退回 intent', () => {
    expect(hitLine('ID', '结论', '意图')).toBe('[ID] 结论')
    expect(hitLine('ID', undefined, '意图')).toBe('[ID] 意图')
    expect(hitLine('ID', '', '意图')).toBe('[ID] 意图')
  })

  it('压成单行并按码点截断到 120', () => {
    expect(hitLine('ID', '结论\n第二行\t带  空白', '意图')).toBe('[ID] 结论 第二行 带 空白')
    const long = '一'.repeat(200)
    const line = hitLine('ID', long, '意图')
    expect(line.endsWith('…')).toBe(true)
    expect([...line.slice('[ID] '.length)]).toHaveLength(120)
  })
})

describe('事件文件解析', () => {
  it('接受 | 块标量形式的多行 outcome', () => {
    const text = [
      '---',
      'id: 2026-08-01_090000',
      'parent: null',
      'kind: fix',
      'status: done',
      'superseded_by: null',
      'intent: 修复端口冲突',
      'anchors:',
      '  commits: []',
      '  files:',
      '  - train/launcher.py',
      'outcome: |-',
      '  第一行',
      '  第二行',
      'lesson: null',
      '---',
      '',
    ].join('\n')
    expect(parseEventHead(text)).toEqual({
      id: '2026-08-01_090000',
      status: 'done',
      intent: '修复端口冲突',
      outcome: '第一行\n第二行',
    })
  })

  it('状态非法或缺必填字段时视为不可用', () => {
    const base = ['---', 'id: X', 'kind: fix', 'status: bogus', 'intent: I', '---', ''].join('\n')
    expect(parseEventHead(base)).toBeUndefined()
    expect(parseEventHead(['---', 'id: X', 'kind: fix', 'status: done', '---', ''].join('\n')))
      .toBeUndefined()
    expect(parseEventHead('no frontmatter')).toBeUndefined()
  })

  it('接受引号形式的标量', () => {
    const text = [
      '---',
      "id: '2026-08-01_090000'",
      'kind: fix',
      'status: done',
      'intent: "带 \\"引号\\" 的意图"',
      "outcome: 'it''s done'",
      '---',
      '',
    ].join('\n')
    expect(parseEventHead(text)).toEqual({
      id: '2026-08-01_090000',
      status: 'done',
      intent: '带 "引号" 的意图',
      outcome: "it's done",
    })
  })
})
