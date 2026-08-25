/**
 * eventmem 的 dsh 宿主适配（B-lite）：TS 侧只做热路径查表与事件转写，
 * `.memory/` 的写入与整理全部 spawn 给 Python 侧的 `eventmem` CLI。
 *
 * 订阅的扩展点（DSH-ADAPTER §2.2）：
 * - `agent/session-start` — 注入工作集，`source === 'compact'` 的重启同样注入；
 * - `tools/result` — 纯观察的锚点浮现，并把机械事实写进 feed；
 * - `session/event` — `todo/write` 的意图浮现，turn/step 边界写进 feed；
 * - `session/flush` — 被 await 的落盘检查点；
 * - `agent/status` — 连续 idle 达阈值后经 `agent.runMaintenance` 拉起抽取与整理；
 * - `ctx.effect` 的 async disposer — 卸载兜底 flush。
 *
 * @module dsh-eventmem
 */

import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { MessageSource } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { ToolExecution, ToolExecutionResult } from '@deepseek-ai/dsh-tools'

import { Config } from './config.js'
import { guard, guardAsync } from './log.js'
import type { LogTarget } from './log.js'
import { MemoryPaths } from './memory.js'
import { asTodos, blocksToText } from './narrow.js'
import { EventmemRuntime } from './runtime.js'
import type { InjectFn } from './runtime.js'

export { Config } from './config.js'
export type { ToolRole } from './config.js'
export { DEFAULT_DELEGATION_TOOLS, DEFAULT_TOOL_NAME_MAP, DEFAULT_TOOL_ROLES } from './config.js'
export type { InjectFn, MaintenanceHost, ToolObservation } from './runtime.js'
export { EventmemRuntime, SessionState } from './runtime.js'
export { MemoryPaths } from './memory.js'
export { errorSignature } from './signature.js'
export { anchorKey, intentTokens, tokenize } from './tokenize.js'
export { relativeToProject } from './relpath.js'

/** 插件名，同时用作注入消息的 `source.plugin`。 */
export const name = 'eventmem'

/**
 * 注入消息的来源标记。`recall` 形态的定义是「从别处日志提取的材料」
 * （`packages/llm/llm/src/message.ts:59`），与 eventmem 的浮现语义一致。
 */
const EVENTMEM_SOURCE: MessageSource = { kind: 'plugin', plugin: name, form: 'recall' }

/**
 * 插件入口。
 *
 * @param ctx - Cordis 上下文。
 * @param config - 已校验的插件配置。
 */
export function apply(ctx: Context, config: Config): void {
  if (!config.enabled) return
  const runtime = new EventmemRuntime(config)
  const agents = new Map<string, Agent>()

  const injectVia = (agent: Agent): InjectFn => (text: string) => {
    agent.inject(createUserMessage({ content: [{ type: 'text', text }], source: EVENTMEM_SOURCE }))
  }

  const cwdOf = (session: Session): string => session.header.cwd ?? process.cwd()

  const agentFor = (session: Session): Agent | undefined =>
    agents.get(session.id) ?? ctx.get('agents')?.get(session.id)

  /**
   * 出错时才解析的日志落点：优先落在该会话的项目下。
   *
   * 取 session 也用 thunk，因此负载对象上任何属性访问的异常都被
   * `resolveTarget` 的 try/catch 兜住，不会绕过监听器护栏。
   */
  const logAt = (getSession: () => Session | undefined): LogTarget => () => {
    let cwd = process.cwd()
    try {
      const session = getSession()
      if (session !== undefined) cwd = cwdOf(session)
    } catch {
      // 连会话都取不到时退回当前目录：dsh 的会话工作目录通常就是它。
    }
    return MemoryPaths.forProject(cwd, config.memoryDirName)
  }

  // ---- 会话启动：注入工作集 ----
  // source 取值为 'startup' | 'resume' | 'clear' | 'compact'；compact 后会重新触发，
  // 因此这一条同时承担了 compact 之后的重新供给。
  ctx.on('agent/session-start', ({ agent }) => {
    guard(logAt(() => agent.session), 'session-start', () => {
      agents.set(agent.session.id, agent)
      runtime.sessionStart(agent.session.id, cwdOf(agent.session), injectVia(agent))
    })
  })

  // ---- 工具结果：纯观察浮现 ＋ feed 落盘 ----
  ctx.on('tools/result', (exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>) => {
    guard(logAt(() => exec.agent?.session), 'tools/result', () => {
      const agent = exec.agent
      if (agent === undefined) return // R-5：非 agent 发起的调用没有注入目标
      runtime.toolResult({
        sessionId: agent.session.id,
        cwd: cwdOf(agent.session),
        toolName: exec.name,
        callId: String(exec.callId),
        args: exec.arguments,
        isError: result.isError,
        value: result.isError ? undefined : result.value,
        errorMessage: result.isError ? result.error.message : undefined,
        contentText: blocksToText(result.content),
      }, injectVia(agent))
    })
    return undefined
  })

  // ---- 会话日志流：todo 快照与 turn/step 边界 ----
  ctx.on('session/event', (session: Session, event: SessionEvent) => {
    guard(logAt(() => session), 'session/event', () => {
      switch (event.type) {
        case 'todo/write': {
          const agent = agentFor(session)
          if (agent === undefined) return
          runtime.todoWrite(session.id, cwdOf(session), asTodos(event.data.todos), injectVia(agent))
          return
        }
        case 'turn/start':
        case 'turn/end':
        case 'step/start':
        case 'step/end':
          runtime.boundary(session.id, cwdOf(session), event.type, { ...event.data, seq: event.seq })
          return
        default:
          return
      }
    })
  })

  // ---- 落盘检查点：全部监听器被 await ----
  ctx.on('session/flush', async (session: Session) => {
    await guardAsync(logAt(() => session), 'session/flush', async () => {
      await runtime.flush(session.id)
    })
  })

  // ---- 空闲整理 ----
  ctx.on('agent/status', ({ agent, status }) => {
    guard(logAt(() => agent.session), 'agent/status', () => {
      if (status === 'idle') runtime.onIdle(agent.session.id, cwdOf(agent.session), agent)
      else runtime.onBusy(agent.session.id)
    })
  })

  // ---- 生命周期收尾 ----
  ctx.on('agent/disposed', ({ agent }) => {
    guard(logAt(() => agent.session), 'agent/disposed', () => {
      agents.delete(agent.session.id)
    })
  })
  ctx.on('session/disposed', (session: Session) => {
    guard(logAt(() => session), 'session/disposed', () => {
      const sessionId = session.id
      void runtime.flush(sessionId).catch(() => undefined).then(() => {
        runtime.drop(sessionId)
        agents.delete(sessionId)
      })
    })
  })

  // ---- 卸载兜底：async disposer 在 fiber 卸载时被 await ----
  ctx.effect(() => async () => {
    await guardAsync(undefined, 'dispose', async () => {
      await runtime.flushAll()
    })
  }, 'eventmem: flush pending feed writes')
}
