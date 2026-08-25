/**
 * `.memory/` 目录布局的只读视图，字段与 `paths.py` 的 `MemoryPaths` 一一对应。
 *
 * B-lite 边界：本插件对 `.memory/` 只读，唯一的写入是 `log/` 下本会话的 seen 文件、
 * feed 文件、浮现／注入埋点文件（SPEC §3.13）与护栏日志。`events/` 与 `index/`
 * 的写入方永远只有 Python。
 *
 * @module
 */

import { homedir } from 'node:os'
import { join } from 'node:path'

import { relativeToProject, resolveProjectDir } from './relpath.js'

/** 一个被管理项目的记忆目录布局。 */
export class MemoryPaths {
  /** 已解析的项目根目录绝对路径。 */
  readonly projectDir: string
  /** 记忆根目录 `<project>/.memory`。 */
  readonly root: string

  private constructor(projectDir: string, memoryDirName: string) {
    this.projectDir = projectDir
    this.root = join(projectDir, memoryDirName)
  }

  /**
   * 由会话工作目录构造记忆路径。
   *
   * @param projectDir - 会话工作目录（`session.header.cwd`）。
   * @param memoryDirName - 记忆目录名，默认 `.memory`。
   * @returns 路径视图。
   */
  static forProject(projectDir: string, memoryDirName = '.memory'): MemoryPaths {
    return new MemoryPaths(resolveProjectDir(projectDir, homedir()), memoryDirName)
  }

  /** L0 事件目录。 */
  get eventsDir(): string {
    return join(this.root, 'events')
  }

  /** L1 索引目录。 */
  get indexDir(): string {
    return join(this.root, 'index')
  }

  /** 护栏日志与水位文件目录。 */
  get logDir(): string {
    return join(this.root, 'log')
  }

  /** 工作集文件。 */
  get workingSet(): string {
    return join(this.indexDir, 'working-set.md')
  }

  /** 全量单行索引文件。 */
  get projectIndex(): string {
    return join(this.indexDir, 'project.md')
  }

  /** 锚点倒排索引文件。 */
  get anchors(): string {
    return join(this.indexDir, 'anchors.json')
  }

  /** 本适配器自己的护栏日志（与 Python 的 eventmem.log 分开，便于分辨来源）。 */
  get adapterLog(): string {
    return join(this.logDir, 'eventmem-dsh.log')
  }

  /**
   * 某个事件的 L0 文件路径。
   *
   * @param eventId - 事件 id。
   * @returns 文件绝对路径。
   */
  eventFile(eventId: string): string {
    return join(this.eventsDir, `${eventId}.md`)
  }

  /**
   * 同会话浮现去重集合的落盘位置，格式与 Python 侧一致（每行一个事件 id）。
   *
   * @param sessionId - 会话 id。
   * @returns 文件绝对路径。
   */
  seenFile(sessionId: string): string {
    return join(this.logDir, `seen-${sessionId}.txt`)
  }

  /**
   * 本适配器写给 Python `eventmem extract` 的 feed 文件。
   *
   * @param sessionId - 会话 id。
   * @returns 文件绝对路径。
   */
  feedFile(sessionId: string): string {
    return join(this.logDir, `dsh-feed-${sessionId}.jsonl`)
  }

  /**
   * 本会话的浮现埋点文件，格式与 Python `post_tool_use.py` 的 `_log_surfaced` 一致
   * （SPEC §3.13）：每行 `{ts, event_id, cue, cue_kind, chars}`。
   *
   * @param sessionId - 会话 id。
   * @returns 文件绝对路径。
   */
  surfacedLogFile(sessionId: string): string {
    return join(this.logDir, `surfaced-${sessionId}.jsonl`)
  }

  /**
   * 本会话的工作集注入埋点文件，格式与 Python `session_start.py` 的 `_log_injected`
   * 一致（SPEC §3.13）：每行 `{ts, source, chars}`。
   *
   * @param sessionId - 会话 id。
   * @returns 文件绝对路径。
   */
  injectedLogFile(sessionId: string): string {
    return join(this.logDir, `injected-${sessionId}.jsonl`)
  }

  /**
   * 把路径规约为项目内的 POSIX 相对路径。
   *
   * @param path - 待规约的路径。
   * @returns 与 Python `MemoryPaths.relative` 一致的文本。
   */
  relative(path: string): string {
    return relativeToProject(this.projectDir, path)
  }
}
