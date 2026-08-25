/**
 * 适配器的全部行为逻辑，与 dsh 的类型解耦。
 *
 * `index.ts` 只负责把 Cordis 的事件负载翻译成这里的窄接口，因此本模块可以在测试里
 * 直接驱动，不需要构造 Agent／Session 实例。
 *
 * @module
 */

import { existsSync, readFileSync } from 'node:fs'

import type { Config } from './config.js'
import { FeedWriter, boundaryRecord, clip, clipFields, toolResultRecord, toolUseRecord } from './feed.js'
import type { FeedTodo } from './feed.js'
import { guardLog } from './log.js'
import { MemoryPaths } from './memory.js'
import { asBashResult, asObject, bashErrorText, firstText, safeName } from './narrow.js'
import { codePointLength } from './pycompat.js'
import { surface } from './recall.js'
import type { CueKind, SurfaceHit } from './recall.js'
import { appendSeen, loadSeen } from './seen.js'
import { errorSignature } from './signature.js'
import { buildArgv, runToCompletion, spawnDetached } from './spawn.js'

/** `.memory/` 存在性复查的间隔（毫秒）：自举后无需重启即可转为可用。 */
const MEMORY_RECHECK_MS = 5_000

/** 注入回调：把一段文本作为 `form: 'recall'` 的用户消息送进模型上下文。 */
export type InjectFn = (text: string) => void

/** 可运行后台整理的宿主句柄，结构上由 dsh 的 `Agent` 满足。 */
export interface MaintenanceHost {
  /**
   * 从真正的空闲相位跑一个非 turn 的维护任务。
   *
   * @param task - 维护任务，接收一个由取消触发的 AbortSignal。
   * @returns 任务的 promise。
   */
  runMaintenance: <T>(task: (signal: AbortSignal) => Promise<T>) => Promise<T>
}

/** 一次工具结果观察所需的全部输入。 */
export interface ToolObservation {
  /** 会话 id（原始值）。 */
  sessionId: string
  /** 会话工作目录。 */
  cwd: string
  /** dsh 工具名。 */
  toolName: string
  /** 调用标识。 */
  callId: string
  /** 已解析入参。 */
  args: unknown
  /** 该调用是否以错误结束。 */
  isError: boolean
  /** 成功时的规范化取值。 */
  value: unknown
  /** 失败时的可读消息。 */
  errorMessage: string | undefined
  /** 模型可见内容拍平后的文本。 */
  contentText: string
}

/** 一次浮现命中及其触发线索，供 §3.13 埋点使用（与 Python `_Surfaced` 对应）。 */
interface CuedHit {
  /** 浮现结果本体。 */
  hit: SurfaceHit
  /** 触发浮现的线索原文。 */
  cue: string
  /** 线索类型。 */
  cueKind: CueKind
}

/** 一个会话的适配器状态。 */
export class SessionState {
  /** 规约后的会话 id：同时用作 feed / seen 文件名与传给 Python 的 `--session`。 */
  readonly id: string
  /** 记忆路径视图。 */
  readonly paths: MemoryPaths
  /** feed 串行追加器。 */
  readonly feed: FeedWriter
  /** 浮现埋点的串行追加器（SPEC §3.13），复用 {@link FeedWriter} 的落盘与护栏风格。 */
  readonly surfacedLog: FeedWriter
  /** 工作集注入埋点的串行追加器（SPEC §3.13）。 */
  readonly injectedLog: FeedWriter
  /** 本会话已浮现过的事件 id。 */
  readonly seen: Set<string>
  /** 自上次整理以来 feed 是否有新内容。 */
  dirty = false
  /** idle 去抖定时器。 */
  idleTimer: ReturnType<typeof setTimeout> | undefined
  /** 是否有整理正在进行。 */
  maintenanceInFlight = false

  private memoryExists = false
  private memoryCheckedAt = 0
  private sequence = 0

  /**
   * @param rawId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @param memoryDirName - 记忆目录名。
   */
  constructor(rawId: string, cwd: string, memoryDirName: string) {
    this.id = safeName(rawId)
    this.paths = MemoryPaths.forProject(cwd, memoryDirName)
    this.feed = new FeedWriter(this.paths.feedFile(this.id), (message) => { guardLog(this.paths, message) })
    this.surfacedLog = new FeedWriter(
      this.paths.surfacedLogFile(this.id),
      (message) => { guardLog(this.paths, `浮现埋点 ${message}`) },
    )
    this.injectedLog = new FeedWriter(
      this.paths.injectedLogFile(this.id),
      (message) => { guardLog(this.paths, `注入埋点 ${message}`) },
    )
    this.seen = loadSeen(this.paths, this.id)
  }

  /** 递增的本地序号，用于生成互不冲突的合成 callId。 */
  nextSequence(): number {
    this.sequence += 1
    return this.sequence
  }

  /**
   * `.memory/` 是否已就绪。结果缓存数秒，使自举完成后无需重启会话即可生效。
   *
   * @returns 记忆根目录是否存在。
   */
  memoryReady(): boolean {
    const now = Date.now()
    if (this.memoryExists) return true
    if (now - this.memoryCheckedAt < MEMORY_RECHECK_MS) return false
    this.memoryCheckedAt = now
    this.memoryExists = existsSync(this.paths.root)
    return this.memoryExists
  }

  /**
   * 记下本次浮现的事件 id，写入 seen 文件并更新内存集合。
   *
   * @param hits - 浮现结果。
   */
  remember(hits: readonly SurfaceHit[]): void {
    const ids = hits.map(hit => hit.eventId)
    for (const id of ids) this.seen.add(id)
    appendSeen(this.paths, this.id, ids)
  }
}

/** 适配器运行时：持有配置与各会话状态。 */
export class EventmemRuntime {
  private readonly sessions = new Map<string, SessionState>()

  /**
   * @param config - 已校验的插件配置。
   */
  constructor(readonly config: Config) {}

  /**
   * 取（或建）一个会话的状态。
   *
   * @param sessionId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @returns 会话状态。
   */
  state(sessionId: string, cwd: string): SessionState {
    const hit = this.sessions.get(sessionId)
    if (hit !== undefined) return hit
    const created = new SessionState(sessionId, cwd, this.config.memoryDirName)
    this.sessions.set(sessionId, created)
    return created
  }

  /** 已建立状态的会话数（测试与诊断用）。 */
  get sessionCount(): number {
    return this.sessions.size
  }

  /**
   * 丢弃一个会话的状态，先取消其 idle 定时器。
   *
   * @param sessionId - 原始会话 id。
   */
  drop(sessionId: string): void {
    const state = this.sessions.get(sessionId)
    if (state === undefined) return
    if (state.idleTimer !== undefined) clearTimeout(state.idleTimer)
    this.sessions.delete(sessionId)
  }

  // ---- 1. 会话启动：注入工作集 ----

  /**
   * 会话启动：`.memory/` 不存在则自举（不阻塞），否则读工作集全文并注入。
   *
   * `source === 'compact'` 的重启同样走这里——compact 之后需要重新供给工作集。
   *
   * @param sessionId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @param inject - 注入回调。
   */
  sessionStart(sessionId: string, cwd: string, inject: InjectFn): void {
    const state = this.state(sessionId, cwd)
    if (!state.memoryReady()) {
      if (this.config.bootstrap) {
        spawnDetached(
          state.paths,
          buildArgv(this.config.pythonExecutable, this.config.pythonModule, ['init'], state.paths.projectDir),
          message => { guardLog(state.paths, `init ${message}`) },
        )
      }
      return
    }
    if (!this.config.injectWorkingSet) return
    let text: string
    try {
      text = readFileSync(state.paths.workingSet, 'utf8')
    } catch {
      return // 缺失或不可读都视为无工作集
    }
    if (text.trim().length === 0) return
    this.logInjected(state, text)
    inject(text)
  }

  /**
   * 注入埋点：追加一行 `log/injected-<session>.jsonl`（SPEC §3.13），格式与 Python
   * `session_start.py` 的 `_log_injected` 一致：`{ts, source: "working-set", chars}`。
   * `chars` 按码点计数，与 Python 的 `len(str)` 同口径。
   *
   * @param state - 会话状态。
   * @param text - 注入的工作集全文。
   */
  private logInjected(state: SessionState, text: string): void {
    state.injectedLog.append({
      ts: new Date().toISOString(),
      source: 'working-set',
      chars: codePointLength(text),
    })
  }

  // ---- 2. 锚点浮现（热路径，进程内查表，不 spawn）----

  /**
   * 工具结果的纯观察处理：按工具名映射浮现，并把机械事实写进 feed。
   *
   * @param observation - 工具结果观察。
   * @param inject - 注入回调。
   */
  toolResult(observation: ToolObservation, inject: InjectFn): void {
    const state = this.state(observation.sessionId, observation.cwd)
    if (!state.memoryReady()) return
    // 委托子 agent 调用（Task/Agent 类）单独走 feed 写入，不参与浮现（SPEC §3.17）。
    if (this.isDelegationTool(observation.toolName)) {
      this.handleDelegationTool(state, observation)
      return
    }
    const role = this.config.toolRoles[observation.toolName]
    if (role === undefined) return
    // todo 类工具由 `session/event` 的 todo/write 快照统一处理，这里不重复。
    if (role === 'todo') return
    if (role === 'file') this.handleFileTool(state, observation, inject)
    else this.handleErrorTool(state, observation, inject)
  }

  private handleFileTool(state: SessionState, observation: ToolObservation, inject: InjectFn): void {
    const found = firstText(asObject(observation.args), this.config.filePathKeys)
    if (found === undefined) return
    const [key, rawPath] = found
    this.writeToolPair(state, observation, { [key]: rawPath })
    const cue = state.paths.relative(rawPath)
    const hits = surface(cue, 'file', state.paths, this.config.surfaceK, state.seen)
    this.emit(state, hits.map(hit => ({ hit, cue, cueKind: 'file' as const })), inject)
  }

  private handleErrorTool(state: SessionState, observation: ToolObservation, inject: InjectFn): void {
    const bash = asBashResult(observation.value)
    const found = firstText(asObject(observation.args), this.config.commandKeys)
    const command = found?.[1]
    this.writeToolPair(state, observation, command === undefined ? {} : { command }, bash)

    const failed = observation.isError || (bash !== undefined && bash.exitCode !== null && bash.exitCode !== 0)
    if (!failed) return
    let text = bash === undefined ? '' : bashErrorText(bash)
    if (text.length === 0) text = observation.errorMessage ?? observation.contentText
    if (text.trim().length === 0) return
    // 预先规范化成签名：既是 surface() 内部对 error 线索的等价处理（幂等），也让
    // 浮现埋点记的 cue 是签名而不是原始多行报错文本，与 Python 侧的记法一致。
    const signature = errorSignature(text)
    if (signature.length === 0) return
    const hits = surface(signature, 'error', state.paths, this.config.surfaceK, state.seen)
    this.emit(state, hits.map(hit => ({ hit, cue: signature, cueKind: 'error' as const })), inject)
  }

  private emit(state: SessionState, cued: readonly CuedHit[], inject: InjectFn): void {
    const capped = cued.slice(0, this.config.surfaceK)
    if (capped.length === 0) return
    const hits = capped.map(item => item.hit)
    state.remember(hits)
    this.logSurfaced(state, capped)
    inject(`Memory:\n${hits.map(hit => hit.line).join('\n')}`)
  }

  /**
   * 浮现埋点：每条命中追加一行 `log/surfaced-<session>.jsonl`（SPEC §3.13），格式
   * 与 Python `post_tool_use.py` 的 `_log_surfaced` 一致：
   * `{ts, event_id, cue, cue_kind, chars}`，`chars` 取注入行（`hit.line`）的码点
   * 长度，与 cued 已经过 K 截断保持一致（记的是真正被注入的那些命中）。
   *
   * @param state - 会话状态。
   * @param cued - 已经过 K 截断的浮现结果，附带各自的触发线索。
   */
  private logSurfaced(state: SessionState, cued: readonly CuedHit[]): void {
    if (cued.length === 0) return
    const ts = new Date().toISOString()
    state.surfacedLog.append(...cued.map(item => ({
      ts,
      event_id: item.hit.eventId,
      cue: item.cue,
      cue_kind: item.cueKind,
      chars: codePointLength(item.hit.line),
    })))
  }

  /**
   * 命中即为委托子 agent 调用的工具名（{@link Config.delegationTools}，小写比较）。
   *
   * @param toolName - dsh 工具名。
   * @returns 是否命中委托工具名单。
   */
  private isDelegationTool(toolName: string): boolean {
    const lowered = toolName.toLowerCase()
    return this.config.delegationTools.some(name => name.toLowerCase() === lowered)
  }

  /**
   * 委托工具（Task/Agent 类子 agent 调用）写入 feed，不参与锚点浮现（SPEC §3.17）。
   *
   * 对齐决策：Python `extract.py` 的机械层按工具名 `Task`／`Agent` 识别委托事件；
   * dsh 侧的委托工具名由 {@link Config.delegationTools} 配置（默认
   * `task`／`subagent`／`agent`），写入 feed 时把匹配到的名字首字母大写化
   * （`task`→`Task`、`agent`→`Agent`，均正好落在 Python 识别的两个名字上；
   * `subagent`→`Subagent` 不在那两个名字之内，是已知的对齐缺口——留给按需在这里
   * 加一张显式映射表，或者在 Python 侧扩一个别名集合）。
   *
   * `arguments` 与 result 文本分别截断：前者逐字段截断（{@link clipFields}），
   * 保留结构给 Python 按字段名取值；后者整体截断（{@link clip}），因为委托事件的
   * `outcome` 直接从返回摘要截取，不能像文件类工具那样折叠成一个 `'ok'` 占位。
   *
   * @param state - 会话状态。
   * @param observation - 工具结果观察。
   */
  private handleDelegationTool(state: SessionState, observation: ToolObservation): void {
    if (!this.config.writeFeed) return
    const name = capitalizeFirst(observation.toolName)
    const args = clipFields(asObject(observation.args) ?? {})
    const resultText = observation.isError
      ? (observation.errorMessage ?? observation.contentText)
      : observation.contentText
    const content = resultText.trim().length > 0 ? clip(resultText) : 'ok'
    state.feed.append(
      toolUseRecord(observation.callId, name, args),
      toolResultRecord(observation.callId, content, observation.isError),
    )
    state.dirty = true
  }

  // ---- 2b. todo 意图浮现 ----

  /**
   * `todo/write` 快照：对 in_progress 的 todo 做意图浮现，并把快照写进 feed。
   *
   * 「同一事件同一会话只浮现一次」由 seen 保证，因此不需要比对上一次快照。
   *
   * @param sessionId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @param todos - 完整 todo 快照。
   * @param inject - 注入回调。
   */
  todoWrite(sessionId: string, cwd: string, todos: readonly FeedTodo[], inject: InjectFn): void {
    const state = this.state(sessionId, cwd)
    if (!state.memoryReady()) return
    if (todos.length === 0) return
    this.writeFeed(state, toolUseRecord(
      `todo-${String(state.nextSequence())}`,
      this.config.toolNameMap['todo_write'] ?? 'TodoWrite',
      { todos: todos.map(todo => ({ content: todo.content, status: todo.status })) },
    ))

    const cued: CuedHit[] = []
    const local = new Set(state.seen)
    for (const todo of todos) {
      if (todo.status !== 'in_progress') continue
      for (const hit of surface(todo.content, 'intent', state.paths, this.config.surfaceK, local)) {
        cued.push({ hit, cue: todo.content, cueKind: 'intent' })
        local.add(hit.eventId)
      }
    }
    this.emit(state, cued, inject)
  }

  // ---- 3. feed 落盘 ----

  /**
   * 写一条 turn/step 边界标记。
   *
   * @param sessionId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @param kind - 边界名，如 `turn/start`。
   * @param data - 边界携带的计数。
   */
  boundary(sessionId: string, cwd: string, kind: string, data: Record<string, unknown>): void {
    const state = this.state(sessionId, cwd)
    if (!state.memoryReady()) return
    this.writeFeed(state, boundaryRecord(kind, data))
  }

  private writeToolPair(
    state: SessionState,
    observation: ToolObservation,
    input: Record<string, unknown>,
    bash?: ReturnType<typeof asBashResult>,
  ): void {
    if (!this.config.writeFeed) return
    const name = this.config.toolNameMap[observation.toolName] ?? observation.toolName
    const structured = bash === undefined
      ? undefined
      : { stdout: clip(bash.stdout), stderr: clip(bash.stderr), interrupted: bash.interrupted }
    const content = bash !== undefined
      ? clip([bash.stdout, bash.stderr].filter(part => part.length > 0).join('\n'))
      : observation.isError
        ? clip(observation.errorMessage ?? observation.contentText)
        : 'ok'
    state.feed.append(
      toolUseRecord(observation.callId, name, input),
      toolResultRecord(observation.callId, content, observation.isError, structured),
    )
    state.dirty = true
  }

  private writeFeed(state: SessionState, record: unknown): void {
    if (!this.config.writeFeed) return
    state.feed.append(record)
    state.dirty = true
  }

  /**
   * 等待某会话 feed／浮现埋点／注入埋点的已排队写入落盘。
   *
   * @param sessionId - 原始会话 id。
   * @returns 落盘完成的 promise。
   */
  async flush(sessionId: string): Promise<void> {
    const state = this.sessions.get(sessionId)
    if (state === undefined) return
    await Promise.all([state.feed.flush(), state.surfacedLog.flush(), state.injectedLog.flush()])
  }

  /**
   * 等待全部会话的 feed／浮现埋点／注入埋点落盘，并取消所有 idle 定时器。用于插件
   * 卸载的兜底。
   *
   * @returns 落盘完成的 promise。
   */
  async flushAll(): Promise<void> {
    const pending: Promise<void>[] = []
    for (const state of this.sessions.values()) {
      if (state.idleTimer !== undefined) clearTimeout(state.idleTimer)
      state.idleTimer = undefined
      pending.push(state.feed.flush(), state.surfacedLog.flush(), state.injectedLog.flush())
    }
    await Promise.all(pending)
  }

  // ---- 4. 整理调度 ----

  /**
   * agent 进入 idle：起去抖定时器；配置秒数内再次转为 running 则取消。
   *
   * @param sessionId - 原始会话 id。
   * @param cwd - 会话工作目录。
   * @param host - 可运行后台整理的宿主句柄。
   */
  onIdle(sessionId: string, cwd: string, host: MaintenanceHost): void {
    if (!this.config.runMaintenance) return
    const state = this.state(sessionId, cwd)
    if (state.idleTimer !== undefined) clearTimeout(state.idleTimer)
    const timer = setTimeout(() => {
      state.idleTimer = undefined
      this.startMaintenance(state, host)
    }, this.config.idleDebounceSeconds * 1000)
    if (typeof timer.unref === 'function') timer.unref()
    state.idleTimer = timer
  }

  /**
   * agent 离开 idle：取消去抖定时器。
   *
   * @param sessionId - 原始会话 id。
   */
  onBusy(sessionId: string): void {
    const state = this.sessions.get(sessionId)
    if (state?.idleTimer === undefined) return
    clearTimeout(state.idleTimer)
    state.idleTimer = undefined
  }

  private startMaintenance(state: SessionState, host: MaintenanceHost): void {
    if (state.maintenanceInFlight || !state.dirty || !state.memoryReady()) return
    state.maintenanceInFlight = true
    try {
      void host.runMaintenance(async signal => this.maintain(state, signal))
        .catch((error: unknown) => {
          guardLog(state.paths, `maintenance 失败 ${error instanceof Error ? error.message : String(error)}`)
        })
        .finally(() => {
          state.maintenanceInFlight = false
        })
    } catch (error) {
      // runMaintenance 在 agent 正被驱动或已有维护任务时同步抛出；下一次 idle 再试。
      state.maintenanceInFlight = false
      guardLog(state.paths, `runMaintenance 拒绝 ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  /**
   * 一次整理：flush feed → `eventmem extract` → `eventmem consolidate --light --deep-if-dirty`。
   *
   * @param state - 会话状态。
   * @param signal - 取消信号。
   */
  async maintain(state: SessionState, signal: AbortSignal): Promise<void> {
    await state.feed.flush()
    state.dirty = false
    const argv = (args: readonly string[]): string[] => buildArgv(
      this.config.pythonExecutable,
      this.config.pythonModule,
      args,
      state.paths.projectDir,
    )

    const extract = await runToCompletion(
      state.paths,
      argv(['extract', '--transcript', state.feed.path, '--session', state.id]),
      signal,
    )
    if (extract.error !== undefined) {
      guardLog(state.paths, `extract 未成功：${extract.error}`)
      return
    }
    if (signal.aborted) return
    const consolidate = await runToCompletion(
      state.paths,
      argv(['consolidate', '--light', '--deep-if-dirty']),
      signal,
    )
    if (consolidate.error !== undefined) guardLog(state.paths, `consolidate 未成功：${consolidate.error}`)
  }
}

/**
 * 把首字符大写化，其余原样保留；空串原样返回。
 *
 * 用于委托工具写入 feed 时的工具名对齐（SPEC §3.17），见 {@link EventmemRuntime}
 * 的 `handleDelegationTool` 上的对齐决策说明。
 *
 * @param text - 原始文本。
 * @returns 首字符大写化后的文本。
 */
function capitalizeFirst(text: string): string {
  if (text.length === 0) return text
  return text.slice(0, 1).toUpperCase() + text.slice(1)
}
