/**
 * 与 Python 侧的互操作集成测试：本插件写出的 feed 必须能被 `eventmem extract` 消化。
 *
 * 这是最重要的一条——它把 B-lite 的分工闭上环：TS 写 feed，Python 读 feed 并独占
 * 写 `.memory/events/`；随后 TS 再读回 Python 写出的事件文件做浮现。
 *
 * 测试直接 spawn 仓库自带的 `.venv/bin/python`。EVENTMEM_API_KEY 被摘掉，
 * `HOME` 指向临时目录，因此 extract 走纯机械模式，结果确定。
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { Config } from '../src/config.js'
import { clearEventCache, readEventHead } from '../src/eventfile.js'
import { MemoryPaths } from '../src/memory.js'
import { clearAnchorCache } from '../src/recall.js'
import { EventmemRuntime } from '../src/runtime.js'
import type { ToolObservation } from '../src/runtime.js'
import { EVENTMEM_DIR, makeProject, pythonExecutable, readIfExists } from './helpers.js'

const SESSION = 'dsh-sess-0001'
const PYTHON = pythonExecutable()

let project: string
let fakeHome: string

interface RunResult { code: number | null, stdout: string, stderr: string }

/**
 * 在受控环境里跑一次 `python -m eventmem.cli ...`。
 *
 * @param args - 子命令及其参数。
 * @returns 退出码与两条流。
 */
async function runCli(...args: string[]): Promise<RunResult> {
  const env: NodeJS.ProcessEnv = { ...process.env, HOME: fakeHome }
  delete env['EVENTMEM_API_KEY']
  delete env['EVENTMEM_BASE_URL']
  delete env['EVENTMEM_MODEL']
  return new Promise<RunResult>((resolve) => {
    const child = spawn(PYTHON ?? 'python3', ['-m', 'eventmem.cli', ...args, '--project', project], {
      cwd: EVENTMEM_DIR,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk: Buffer) => { stdout += chunk.toString('utf8') })
    child.stderr.on('data', (chunk: Buffer) => { stderr += chunk.toString('utf8') })
    child.on('close', (code) => { resolve({ code, stdout, stderr }) })
    child.on('error', () => { resolve({ code: null, stdout, stderr }) })
  })
}

/**
 * 列出 Python 写下的 L0 事件文件全文。
 *
 * @returns 文件名到全文的映射。
 */
function events(): Record<string, string> {
  const dir = join(project, '.memory', 'events')
  if (!existsSync(dir)) return {}
  const out: Record<string, string> = {}
  for (const name of readdirSync(dir)) out[name] = readFileSync(join(dir, name), 'utf8')
  return out
}

function observation(overrides: Partial<ToolObservation>): ToolObservation {
  return {
    sessionId: SESSION,
    cwd: project,
    toolName: 'read',
    callId: 'call',
    args: {},
    isError: false,
    value: undefined,
    errorMessage: undefined,
    contentText: '',
    ...overrides,
  }
}

/**
 * 走一遍典型会话：开 todo → 改文件 → 跑测试失败 → 提交 → 关 todo。
 *
 * @param runtime - 适配器运行时。
 */
function driveSession(runtime: EventmemRuntime): void {
  const noop = (): void => { /* 本测试不关心注入内容 */ }
  runtime.boundary(SESSION, project, 'turn/start', { turn: 1, seq: 1 })
  runtime.todoWrite(SESSION, project, [{ content: '修复 Ray 端口冲突', status: 'in_progress' }], noop)
  runtime.toolResult(observation({
    toolName: 'edit',
    callId: 'edit-1',
    args: { file_path: join(project, 'train', 'launcher.py'), old_string: 'a', new_string: 'b' },
    contentText: '文件已更新',
  }), noop)
  runtime.toolResult(observation({
    toolName: 'bash',
    callId: 'bash-1',
    args: { command: 'pytest tests/test_launcher.py' },
    value: {
      exitCode: 1,
      stdout: { text: '', truncated: false },
      stderr: {
        text: [
          'Traceback (most recent call last):',
          '  File "launcher.py", line 10, in <module>',
          "    raise ValueError('port busy')",
          'ValueError: port busy',
        ].join('\n'),
        truncated: false,
      },
    },
  }), noop)
  runtime.toolResult(observation({
    toolName: 'bash',
    callId: 'bash-2',
    args: { command: "git commit -am 'fix: port conflict'" },
    value: {
      exitCode: 0,
      stdout: { text: '[main a3f21c9] fix: port conflict\n 1 file changed', truncated: false },
      stderr: { text: '', truncated: false },
    },
  }), noop)
  runtime.todoWrite(SESSION, project, [{ content: '修复 Ray 端口冲突', status: 'completed' }], noop)
  runtime.boundary(SESSION, project, 'turn/end', { turn: 1, reason: 'completed', seq: 9 })
}

beforeEach(() => {
  project = makeProject('eventmem-interop-')
  fakeHome = mkdtempSync(join(tmpdir(), 'eventmem-home-'))
  clearAnchorCache()
  clearEventCache()
})

afterEach(() => {
  rmSync(project, { recursive: true, force: true })
  rmSync(fakeHome, { recursive: true, force: true })
})

describe.skipIf(PYTHON === undefined)('feed 与 Python extract 的互操作', () => {
  it('TS 写出的 feed 被 eventmem extract 消化成事件，锚点齐全', async () => {
    const runtime = new EventmemRuntime(Config({}))
    driveSession(runtime)
    await runtime.flush(SESSION)

    const feed = join(project, '.memory', 'log', `dsh-feed-${SESSION}.jsonl`)
    expect(existsSync(feed)).toBe(true)
    // 每一行都必须是合法 JSON，否则 Python 会计入 skipped_lines
    for (const line of readFileSync(feed, 'utf8').split('\n').filter(l => l.length > 0)) {
      expect(() => JSON.parse(line)).not.toThrow()
    }

    const result = await runCli('extract', '--transcript', feed, '--session', SESSION)
    expect(result.code, `stderr: ${result.stderr}`).toBe(0)
    expect(result.stdout).toContain('新增事件: 1')

    const written = Object.values(events())
    expect(written).toHaveLength(1)
    const text = written[0] ?? ''
    expect(text).toContain('intent: 修复 Ray 端口冲突')
    expect(text).toContain('status: open') // 机械层只开事件，闭合由轻整理负责
    expect(text).toContain('train/launcher.py')
    expect(text).toContain('a3f21c9')
    expect(text).toContain('pytest tests/test_launcher.py')
    expect(text).toContain('ValueError: port busy')
    expect(text).toContain(`${SESSION}#L`)
  })

  it('Python 写出的事件文件能被 TS 侧解析并浮现', async () => {
    const runtime = new EventmemRuntime(Config({}))
    driveSession(runtime)
    await runtime.flush(SESSION)
    const feed = join(project, '.memory', 'log', `dsh-feed-${SESSION}.jsonl`)
    expect((await runCli('extract', '--transcript', feed, '--session', SESSION)).code).toBe(0)
    expect((await runCli('rebuild')).code).toBe(0)

    const paths = MemoryPaths.forProject(project)
    const [name] = Object.keys(events())
    const head = readEventHead(paths.eventFile((name ?? '').replace(/\.md$/u, '')))
    expect(head?.intent).toBe('修复 Ray 端口冲突')
    expect(head?.status).toBe('open')

    // Python rebuild 写出的 anchors.json 必须能被 TS 的规约查中
    const anchors = JSON.parse(readFileSync(paths.anchors, 'utf8')) as Record<string, string[]>
    expect(Object.keys(anchors)).toContain('file:train/launcher.py')
    expect(Object.keys(anchors)).toContain('error:ValueError: port busy')

    const fresh = new EventmemRuntime(Config({}))
    const injected: string[] = []
    fresh.toolResult(observation({
      toolName: 'read',
      callId: 'read-1',
      args: { file_path: join(project, 'train', 'launcher.py') },
    }), text => injected.push(text))
    expect(injected[0]).toContain('修复 Ray 端口冲突')
  })

  it('水位生效：同一 feed 二次 extract 不重复开事件', async () => {
    const runtime = new EventmemRuntime(Config({}))
    driveSession(runtime)
    await runtime.flush(SESSION)
    const feed = join(project, '.memory', 'log', `dsh-feed-${SESSION}.jsonl`)

    expect((await runCli('extract', '--transcript', feed, '--session', SESSION)).stdout)
      .toContain('新增事件: 1')
    expect((await runCli('extract', '--transcript', feed, '--session', SESSION)).stdout)
      .toContain('新增事件: 0')
    expect(Object.keys(events())).toHaveLength(1)
  })

  it('maintain() 串起 extract 与 consolidate，产出工作集', async () => {
    const previousHome = process.env['HOME']
    const previousKey = process.env['EVENTMEM_API_KEY']
    process.env['HOME'] = fakeHome
    delete process.env['EVENTMEM_API_KEY']
    try {
      const runtime = new EventmemRuntime(Config({ pythonExecutable: PYTHON ?? 'python3' }))
      driveSession(runtime)
      const state = runtime.state(SESSION, project)
      await runtime.maintain(state, new AbortController().signal)

      expect(Object.keys(events())).toHaveLength(1)
      const paths = MemoryPaths.forProject(project)
      expect(readIfExists(paths.workingSet)).toContain('修复 Ray 端口冲突')
      expect(readIfExists(paths.projectIndex)).toContain('| id | kind | status | intent |')
      expect(readIfExists(paths.adapterLog)).not.toContain('未成功')
    } finally {
      if (previousHome === undefined) delete process.env['HOME']
      else process.env['HOME'] = previousHome
      if (previousKey !== undefined) process.env['EVENTMEM_API_KEY'] = previousKey
    }
  })

  it('Python 侧不可用时 maintain 只记日志，不抛异常', async () => {
    const runtime = new EventmemRuntime(Config({ pythonExecutable: '/nonexistent/python-xyz' }))
    driveSession(runtime)
    const state = runtime.state(SESSION, project)
    await expect(runtime.maintain(state, new AbortController().signal)).resolves.toBeUndefined()
    expect(readIfExists(MemoryPaths.forProject(project).adapterLog)).toContain('extract 未成功')
  })
})
