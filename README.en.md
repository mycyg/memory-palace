# MemoryPalace

> An event-based memory system for coding agents. The event is the atomic unit of memory: each unit of work is recorded as a closed loop of intent → action → outcome. Raw records are immutable, indexes are rebuildable, consolidation runs offline, and injection is bounded by a fixed budget.

[中文](README.md) | **English** | [日本語](README.ja.md)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![TypeScript](https://img.shields.io/badge/TypeScript-3178C6.svg) ![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)

MemoryPalace is the project's name; the package names didn't change with the rebrand. The Python package is still `eventmem` (`name = "eventmem"` in `pyproject.toml`, same name for the CLI entry point), and the DeepSeek Harness plugin is still `dsh-eventmem`.

MemoryPalace is long-term memory for LLM agents that survives across coding sessions. Instead of compacting history into a lossy summary, it keeps each unit of work as an immutable event — a closed loop of episodic memory — and recalls it through anchor-triggered associative surfacing, with every injection bounded by a fixed context-management budget.

## Where the Name Comes From

MemoryPalace borrows its name from the method of loci — the memory-palace technique: you hang the things you need to remember on fixed spots along a path you know well, and later, walking that path again, arriving at a spot brings back whatever you hung there, with no active search required. That's the classical version of this system's anchor-triggered surfacing — the cue isn't queried for, it comes up because you arrived at the place. Cicero recorded the technique's origin and use in *De Oratore*; memory athletes today still use the same spatial encoding to memorize a shuffled deck of cards or long strings of digits in competition. This system swaps "places" for file paths, error signatures, and todo intents — same anchor, same trigger.

## Three Positions

1. **Closed-loop events are the atom.** No knowledge points, no chunks. An event = intent → action → outcome (done / abandoned / superseded), self-contained, injected and evicted as a whole. A lesson is distilled from an event; it isn't a separate module.
2. **Associative surfacing first, retrieval as fallback — a RAG alternative to query-based recall.** The primary recall path is anchor-triggered, active surfacing: when the agent opens a file, hits an error, or starts a todo, the one-line outcome of a matching past event shows up in context on its own — the agent never has to know it should look something up. Anchors match exactly (file path / error signature / intent token); no embeddings, no vector store. Query-based retrieval only serves explicit lookups.
3. **Prefer misses over bloat.** Injection is capped by hard budgets (working set 1–2k tokens, ≤3 lines per surfacing event, deduplicated within a session). Misses heal themselves: miss → mistake → the mistake gets recorded → it's in the index next time. Bloat doesn't heal itself.

## Positioning

The three systems MemoryPalace gets compared to most, and the difference in one line each:

| Compared to | How MemoryPalace differs |
|---|---|
| Query-based RAG | Primary recall is anchor-triggered active surfacing (push), not query → embedding → top-k retrieval (pull); retrieval only serves explicit lookups |
| MemGPT / Letta | No autonomous model-driven paging; injection is bounded by a fixed budget and the working set |
| Mem0 / fact stores | The atom of memory is a closed-loop event (intent → action → outcome), not an isolated fact extracted from conversation |

## Architecture

```
┌─────────────────────────────────────────────────┐
│ L2 注入层（进上下文，固定预算）                     │
│   会话启动注入工作集 ＋ 行为流触发的联想浮现          │
├─────────────────────────────────────────────────┤
│ L1 索引层（派生，可整体重建）                       │
│   working-set / 全量单行索引 / 锚点倒排 / lesson 表 │
├─────────────────────────────────────────────────┤
│ L0 存储层（append-only，不可变）                   │
│   一事件一 markdown 文件，锚点指向对话原文           │
└─────────────────────────────────────────────────┘
```

Writing and consolidation are separate: during a session, events are only appended (flush); consolidation — closing dangling events, backfilling outcomes, distilling lessons, promotion/retirement, rebuilding indexes and prefetch — happens entirely offline. Light consolidation runs in the background at the end of every session; deep consolidation triggers once enough unconsolidated events pile up. Consolidation only ever writes to the derived layer and the lesson field, so it's idempotent and safe to rerun: if a dream goes wrong, the day's memories are still there.

Full design in [DESIGN.md](DESIGN.md); the implementation contract is in [SPEC.md](SPEC.md).

## Two Hosts, One Memory

The same `.memory/` directory can be shared by two hosts:

- **Claude Code** — read and written by Python hooks (`SessionStart` / `PostToolUse` / `PreCompact` / `SessionEnd`)
- **DeepSeek Harness (dsh)** — the native Cordis plugin [`dsh-eventmem`](dsh-plugin/README.md), built on a B-lite architecture: TypeScript only handles the synchronous hot path (surfacing lookups, transcribing events to a feed); writing to `.memory/` and offline consolidation are still delegated to the same Python `eventmem` CLI as a child process, so there's never a second implementation to keep in sync

`.memory/` follows the project, not the tool. A mistake caught in Claude Code yesterday surfaces the same way when you open that file in dsh today, and vice versa. Both hosts share one event store, one anchor inverted index, one lesson table, and one same-session dedup set.

## Installation

### Claude Code

```bash
pip install -e .
```

Merge the four hooks from [examples/settings.json](examples/settings.json) into the project's `.claude/settings.json` (or the global `~/.claude/settings.json`):

- `SessionStart` — injects the working set; bootstraps silently when `.memory/` doesn't exist yet (zero config — memory starts accumulating the moment it's installed)
- `PostToolUse` — anchor surfacing (a pure table lookup, millisecond-scale, no LLM call)
- `PreCompact` — rescue extraction right before compaction (background)
- `SessionEnd` — a mechanical flush plus background light consolidation (deep consolidation once enough events have piled up)

The LLM step (backfilling missed events, filling in outcomes, distilling lessons) is an optional enhancement: point it at an Anthropic-compatible endpoint to turn it on; without one, it degrades to a rules-only mode and the pipeline keeps working regardless. Configure it in `.env` at the project root, or in `~/.claude/eventmem.env` (see [.env.example](.env.example)):

```bash
EVENTMEM_BASE_URL=https://api.anthropic.com   # 任何 Anthropic 兼容端点均可
EVENTMEM_API_KEY=sk-...
EVENTMEM_MODEL=claude-haiku-4-5-20251001      # 小模型足够
```

Verified compatible endpoints: Anthropic's own API, and DeepSeek (`https://api.deepseek.com/anthropic`).

### DeepSeek Harness (dsh)

```bash
dsh plugin --profile <name> add dsh-eventmem
```

Requires a host machine that can `import eventmem` (the Python side handles the actual writes and consolidation). Configuration options, Model Experience, and known limitations are in [dsh-plugin/README.md](dsh-plugin/README.md).

## CLI

```
eventmem status                  # 事件数、open 数、脏量、索引年龄、未读 CLAUDE.md 建议
eventmem stats [--json]          # 评估指标：浮现采纳率、注入成本、重复踩坑、预取命中、分级计数
eventmem log [--tree|--since N|--kind K]   # 事件时间线视图
eventmem search <query> [--all]  # 兜底检索（BM25）；--all 附带搜归档层（不解包）
eventmem read <id>               # 事件全文；命中冻结事件时提示 thaw
eventmem trace <id>              # 对话原文指针
eventmem consolidate --light|--deep|--deep-if-dirty
eventmem rebuild                 # 全量重建索引（L1 可随时丢弃重建）
eventmem thaw <epoch|id>         # 解冻归档事件回活跃层
eventmem purge --before <date>   # 删除更早的冻结包（默认 --dry-run，需 --yes）
eventmem init                    # 手动建 .memory/ 骨架（通常不需要，hook 会自举）
```

## Diagnostics

Hooks follow one rule above all others: never interrupt the host session. Any exception exits silently — even the example config's `2>/dev/null || true` swallows a missing package. The cost is that failures go unnoticed too; here's how to check:

```bash
eventmem status                                      # 包与数据是否正常
echo '{"cwd":"'$PWD'"}' | python3 -m eventmem.hooks.session_start   # 手动跑一个 hook 看输出
tail -20 .memory/log/eventmem.log                    # 护栏日志（hook 异常都在这里）
```

## v0.2 at a Glance

On top of v0.1's closed-loop events, three-layer architecture, and associative surfacing:

- **Salience**: rules draw the hard boundaries, self-assessment sets the starting value, evidence has the final say — importance is a derived quantity; deep consolidation recomputes it from evidence (citations, surfacing accepted/ignored, triggering a supersede) to decide which tier something lives in, not whether it gets kept at all
- **Negative feedback**: surfacing that gets ignored is automatically down-weighted; bad memories sink on their own
- **Prefetch**: consolidation ends by predicting the next session's entry point (prospective markers like "next:", associates of open events, model-level prediction), so the working set carries a slice of the future tense
- **Tiered forgetting**: hot → cold (evicted from the index) → frozen (quarterly epoch summaries plus a full archive bundle, disk use down to roughly a fifth) → purge (manual only). Compresses access structure, not information — the original text stays intact inside the bundle, and `thaw` restores it anytime
- **Adaptive granularity**: fragmented events get grouped, coarse events get virtual sub-segments — all at the index layer, with L0 staying immutable
- **Sub-agent attribution**: delegated calls are recorded as delegation events, symmetric across both hosts
- **Cross-project promotion**: after clearing a portability check and sensitive-data scrubbing, a lesson can be promoted to the user level
- **CLAUDE.md suggestions**: a lesson confirmed repeatedly generates a paste-ready suggestion; the system never edits the user's own docs automatically
- **Sensitive-data scrubbing**: seven classes of secret patterns are intercepted before anything is written to L0
- **Evaluation instrumentation**: surfacing, injection, and prefetch are logged locally as jsonl throughout the pipeline; `eventmem stats` reports four metrics

## Status

v0.2. 165+ tests on the Python side (including real-LLM integration and archive fault injection), 134 on the dsh plugin (including cross-language interop); two end-to-end session rehearsals cover the full hook pipeline. Known limitations and open questions are in [DESIGN.md §8](DESIGN.md).

`.memory/` is human-readable markdown — a glass box with zero maintenance obligation: look whenever you want, and it keeps working even when you don't.
