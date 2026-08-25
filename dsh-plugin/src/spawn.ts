/**
 * 拉起 Python 侧的 `eventmem` CLI。
 *
 * B-lite 分工：`.memory/events/` 与 `.memory/index/` 的写入方永远只有 Python，
 * TS 侧一切写入与整理都通过子进程走这里。子进程继承本进程环境变量（Python 侧的
 * `_load_env` 会再从 cwd 的 `.env` 与 `~/.claude/eventmem.env` 补齐 EVENTMEM_*）。
 *
 * @module
 */

import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { createWriteStream, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

import type { MemoryPaths } from './memory.js'

/** 子进程的一次运行结果。 */
export interface RunOutcome {
  /** 退出码；被信号杀死或拉起失败时为 null。 */
  code: number | null
  /** 拉起或运行过程中的错误描述；成功时为 undefined。 */
  error: string | undefined
}

/**
 * 拼出 `python -m eventmem.cli <args...> --project <dir>` 的完整参数表。
 *
 * `--project` 放在子命令参数之后，与 Python 侧 hooks 的调用形态一致
 * （argparse 的 `--project` 同时挂在顶层与每个子解析器上）。
 *
 * @param executable - Python 可执行程序。
 * @param moduleName - Python 模块名。
 * @param args - 子命令及其参数。
 * @param projectDir - 项目根目录。
 * @returns argv 数组，首项为可执行程序。
 */
export function buildArgv(
  executable: string,
  moduleName: string,
  args: readonly string[],
  projectDir: string,
): string[] {
  return [executable, '-m', moduleName, ...args, '--project', projectDir]
}

/**
 * 后台拉起，不等待完成；失败只记日志。用于会话启动时的自举。
 *
 * @param paths - 记忆路径视图，决定日志落点。
 * @param argv - 完整 argv，首项为可执行程序。
 * @param onError - 失败时的日志回调。
 */
export function spawnDetached(
  paths: MemoryPaths,
  argv: readonly string[],
  onError: (message: string) => void,
): void {
  const [executable, ...rest] = argv
  if (executable === undefined) return
  try {
    mkdirSync(dirname(paths.adapterLog), { recursive: true })
    const sink = createWriteStream(paths.adapterLog, { flags: 'a' })
    sink.on('error', () => { /* 日志写入失败静默 */ })
    const child = spawn(executable, rest, {
      cwd: paths.projectDir,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      detached: true,
    })
    child.stdout?.pipe(sink)
    child.stderr?.pipe(sink)
    child.on('error', (error: Error) => { onError(`spawn 失败 ${error.message}`) })
    child.on('close', () => { sink.end() })
    child.unref()
  } catch (error) {
    onError(`spawn 失败 ${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * 前台拉起并等待完成，用于 `agent.runMaintenance` 内部。
 *
 * 永不 reject：拉起失败与非零退出都以 {@link RunOutcome} 返回，由调用方记日志。
 *
 * @param paths - 记忆路径视图，决定 cwd 与日志落点。
 * @param argv - 完整 argv，首项为可执行程序。
 * @param signal - 取消信号；触发时向子进程发 SIGTERM。
 * @returns 运行结果。
 */
export async function runToCompletion(
  paths: MemoryPaths,
  argv: readonly string[],
  signal: AbortSignal,
): Promise<RunOutcome> {
  const [executable, ...rest] = argv
  if (executable === undefined) return { code: null, error: 'argv 为空' }
  if (signal.aborted) return { code: null, error: '已取消' }
  return new Promise<RunOutcome>((resolve) => {
    let settled = false
    const finish = (outcome: RunOutcome): void => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', onAbort)
      resolve(outcome)
    }
    let child: ChildProcess | undefined
    try {
      mkdirSync(dirname(paths.adapterLog), { recursive: true })
      const sink = createWriteStream(paths.adapterLog, { flags: 'a' })
      sink.on('error', () => { /* 日志写入失败静默 */ })
      child = spawn(executable, rest, {
        cwd: paths.projectDir,
        env: process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      child.stdout?.pipe(sink)
      child.stderr?.pipe(sink)
      child.on('close', (code: number | null) => {
        sink.end()
        finish({ code, error: code === 0 ? undefined : `退出码 ${String(code)}` })
      })
      child.on('error', (error: Error) => {
        sink.end()
        finish({ code: null, error: error.message })
      })
    } catch (error) {
      finish({ code: null, error: error instanceof Error ? error.message : String(error) })
      return
    }
    function onAbort(): void {
      try {
        child?.kill('SIGTERM')
      } catch {
        // 子进程已退出
      }
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}
