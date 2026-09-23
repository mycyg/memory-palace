/** DeepSeek Harness adapter using the shared MemoryPalace HTTP service. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { MessageSource } from '@deepseek-ai/dsh-llm'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { ToolExecution, ToolExecutionResult } from '@deepseek-ai/dsh-tools'

import { Config } from './config.js'
import { ServiceRuntime } from './service-runtime.js'
import type { InjectFn } from './service-runtime.js'

export { Config } from './config.js'
export { ServiceRuntime } from './service-runtime.js'
export type { InjectFn, ToolObservation } from './service-runtime.js'

export const name = 'eventmem'
const SOURCE: MessageSource = { kind: 'plugin', plugin: name, form: 'recall' }

function textOf(blocks: readonly unknown[]): string {
  return blocks.flatMap((raw) => {
    if (typeof raw !== 'object' || raw === null) return []
    const block = raw as Record<string, unknown>
    return block.type === 'text' && typeof block.text === 'string' ? [block.text] : []
  }).join('\n')
}

function safe(body: () => void): void {
  try { body() } catch { /* host availability takes precedence */ }
}

async function safeAsync(body: () => Promise<void>): Promise<void> {
  try { await body() } catch { /* durable spool retains any created event */ }
}

export function apply(ctx: Context, config: Config): void {
  if (!config.enabled) return
  const runtime = new ServiceRuntime(config)
  const agents = new Map<string, Agent>()
  const cwdOf = (session: Session) => session.header.cwd ?? process.cwd()
  const injectVia = (agent: Agent): InjectFn => (text) => {
    agent.inject(createUserMessage({ content: [{ type: 'text', text }], source: SOURCE }))
  }
  const agentFor = (session: Session): Agent | undefined =>
    agents.get(session.id) ?? ctx.get('agents')?.get(session.id)

  ctx.on('agent/session-start', ({ agent, source }) => safe(() => {
    agents.set(agent.session.id, agent)
    runtime.sessionStart(agent.session.id, cwdOf(agent.session), injectVia(agent), source)
  }))

  ctx.on('tools/execute', async (exec, next) => {
    const agent = exec.agent
    if (agent) await safeAsync(() => runtime.preAction(
      agent.session.id, cwdOf(agent.session), exec.name, exec.arguments, injectVia(agent)
    ))
    return next()
  })

  ctx.on('tools/result', (exec: Readonly<ToolExecution>, result: Readonly<ToolExecutionResult>) => {
    safe(() => {
      const agent = exec.agent
      if (!agent) return
      runtime.toolResult({
        sessionId: agent.session.id,
        cwd: cwdOf(agent.session),
        toolName: exec.name,
        callId: String(exec.callId),
        args: exec.arguments,
        isError: result.isError,
        value: result.isError ? undefined : result.value,
        errorMessage: result.isError ? result.error.message : undefined,
        contentText: textOf(result.content),
      }, injectVia(agent))
    })
    return undefined
  })

  ctx.on('session/event', (session: Session, event: SessionEvent) => safe(() => {
    const cwd = cwdOf(session)
    switch (event.type) {
      case 'user/message':
        if (event.data.source.kind === 'user')
          runtime.message(session.id, cwd, 'user', textOf(event.data.content), event.seq)
        break
      case 'assistant/message':
        runtime.message(session.id, cwd, 'assistant', textOf(event.data.message.content), event.seq)
        break
      case 'todo/write': {
        const agent = agentFor(session)
        if (agent) runtime.todoWrite(session.id, cwd, event.data.todos, injectVia(agent))
        break
      }
      case 'turn/start':
      case 'turn/end':
      case 'step/start':
      case 'step/end':
        runtime.boundary(session.id, cwd, event.type, { ...event.data, seq: event.seq })
        break
    }
  }))

  ctx.on('session/flush', async (session: Session) => {
    await safeAsync(() => runtime.flush(session.id))
  })
  ctx.on('agent/disposed', ({ agent }) => safe(() => {
    agents.delete(agent.session.id)
  }))
  ctx.on('session/disposed', (session: Session) => safe(() => {
    void runtime.flush(session.id).catch(() => undefined).then(async () => {
      runtime.drop(session.id)
      agents.delete(session.id)
      await runtime.flush(session.id)
    })
  }))
  ctx.effect(() => async () => {
    await safeAsync(() => runtime.flushAll())
  }, 'eventmem: flush durable host events')
}
