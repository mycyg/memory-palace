# MemoryPalace

> Give your AI coding agent long-term memory: it remembers what it did, what tripped it up, and what it promised you.

[中文](README.md) | **English** | [日本語](README.ja.md)

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![TypeScript](https://img.shields.io/badge/TypeScript-3178C6.svg) ![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)

MemoryPalace is a long-term memory system for agents that survives across sessions — global agent memory built on events. **The event is the atom of memory**: every piece of work is recorded as a closed loop of intent → action → outcome; raw records are immutable, indexes are rebuildable, consolidation runs offline, and injection is budget-capped.

## What This Is

AI coding agents have a chronic problem: close the session, and everything's gone.

The bug that took you all afternoon to fix yesterday — it hits the same one today, the same way. The rule you've already explained three times — you're explaining it a fourth. The approach that dead-ended last month — next week it tries the exact same thing again, just as pleased with itself.

MemoryPalace fixes that. It's long-term memory for LLM agents: once it's installed —

- Everything the agent does gets recorded automatically as an **event**: what it was trying to do, what it did, how it turned out
- Next time it opens the same file or hits the same error, the relevant history **surfaces in front of it automatically** — it doesn't have to remember to look, and you don't have to remind it
- You do nothing: no notes to write, no tags to apply, no upkeep. It records, organizes, and forgets what doesn't matter, all in the background

In one line: **the agent gets better at your project the more you use it.**

## How It Works (30-Second Version)

1. **Record** — while the agent works, editing files, running commands, updating its todo list, each action is automatically logged as an event.
2. **Sleep** — once the session ends, it "sleeps" in the background: archiving the loose records, distilling lessons, and taking a guess at what you'll probably want to do tomorrow.
3. **Surface** — a new session starts with yesterday's unfinished work already on the table; open a file, and whatever tripped it up here before comes back on its own.

Before installing it:

> You: get the training jobs running in parallel
> Agent: On it! (launch failed: port conflict — same problem as last month, twenty minutes of debugging again)

After installing it:

> You: get the training jobs running in parallel
> Agent: (memory surfaces — `[2026-07-14] parallel jobs claimed the same port, fixed by offsetting by job id`)
> Sure — I'll offset the ports by job id, learned that one the hard way.

## Quick Start

### Claude Code

```bash
pip install -e .
```

Merge the four Claude Code hooks from [examples/settings.json](examples/settings.json) into your project's `.claude/settings.json` (or the global `~/.claude/settings.json`) — it's live as soon as you install it:

- `SessionStart` — hands the agent the "working set" (the one screen's worth it should remember) at boot; the first run auto-creates `.memory/`, zero config
- `PostToolUse` — surfaces old memory by anchor while the agent works (a pure table lookup, millisecond-scale, no model call)
- `PreCompact` — rescues events to disk right before context gets compacted (background)
- `SessionEnd` — triggers background consolidation once the session ends

To make consolidation smarter (auto-filling outcomes, distilling lessons), point it at a model — it also runs fine unconfigured, in rules-only mode. Set this in `.env` at the project root, or in `~/.claude/eventmem.env` (see [.env.example](.env.example)):

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

The DeepSeek Harness plugin needs a host that can `import eventmem` (the Python side handles the actual writing and consolidation). Configuration options and known limitations are in [dsh-plugin/README.md](dsh-plugin/README.md).

**Both hosts share the same memory**: a problem you hit in Claude Code yesterday surfaces the same way when you open that file in dsh today. `.memory/` follows the project, not the tool.

## Where the Name Comes From

The method of loci — the memory palace technique — is a 2,000-year-old memorization trick: you hang the things you need to remember on fixed spots along a path you know well, and later, walking that path again, whatever you hung at a spot comes back to you the moment you arrive there. No searching, no trying to recall — you get there, and it's just there. Cicero used it to memorize speeches; competitive memory athletes today use it to memorize a full deck of cards.

This system swaps "spots" for file paths, error signatures, and todo intents. Same anchor, same trigger.

## Why It's Built This Way: Three Positions

1. **Closed-loop events are the atom.** No loose knowledge points, no chunking. One event = intent → action → outcome (done / abandoned / superseded) — self-contained, injected and evicted as a whole, the atomic unit of episodic memory. A lesson is distilled out of an event; it isn't a separate module.
2. **Associative surfacing first, retrieval as fallback.** The primary recall path is anchor-triggered active surfacing — a RAG alternative to query-based recall: open a file, hit an error, start a todo, and the outcome of a matching past event shows up on its own. It doesn't depend on the agent knowing what to look up (the whole nature of a pitfall is that you don't know it's there until you're in it). Anchors match exactly — no embeddings, no vector store. Query-based retrieval only serves explicit digging through history.
3. **Prefer misses over bloat.** Injection is capped by a hard context-management budget (working set 1–2k tokens, ≤3 lines per surfacing event, deduplicated within a session). A miss heals itself: miss → mistake → the mistake gets recorded → it's there next time. Bloat doesn't heal itself.

## How It Differs from Similar Systems

The three approaches to agent memory MemoryPalace gets compared to most, and the difference in one line each:

| Compared to | How MemoryPalace differs |
|---|---|
| Query-based RAG | Primary recall is anchor-triggered active surfacing (push), not query → embedding → top-k retrieval (pull); retrieval only serves explicit digging through history |
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

Writing and consolidation are kept separate: during a session, everything is append-only; consolidation — closing dangling events, backfilling outcomes, distilling lessons, promotion and retirement, rebuilding the index and prefetch — happens entirely offline, as part of the memory-consolidation pipeline. Light consolidation runs in the background at the end of every session; deep consolidation triggers once enough events have piled up. Consolidation only ever writes to the derived layer and the lesson field, so it's idempotent and safe to rerun: if the dream goes wrong, the day's memories are still there.

Full design in [DESIGN.md](DESIGN.md); the implementation contract is in [SPEC.md](SPEC.md).

## CLI

```
eventmem status                  # 事件数、open 数、积压量、索引年龄、未读 CLAUDE.md 建议
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

The one rule hooks follow above everything else: never interrupt your normal session. Any exception exits silently. The cost is that failures go unnoticed too — three commands to check:

```bash
eventmem status                                      # 包与数据是否正常
echo '{"cwd":"'$PWD'"}' | python3 -m eventmem.hooks.session_start   # 手动跑一个 hook 看输出
tail -20 .memory/log/eventmem.log                    # 护栏日志（hook 异常都在这里）
```

## v0.2 at a Glance

On top of closed-loop events, the three-layer architecture, and associative surfacing:

- **Salience**: rules draw the hard boundaries, self-assessment sets the starting value, evidence has the final say — importance is recomputed offline from evidence (citations, surfacing accepted or ignored, triggering a supersede), deciding which tier a memory lives in, not whether it's kept at all
- **Negative feedback**: surfacing that gets ignored is automatically down-weighted; bad memories sink on their own
- **Prefetch**: consolidation ends by predicting the next session's entry point (a "do X next" prospective marker, associates of unfinished events, model-level prediction), so the opening screen already carries a guess at what's next
- **Tiered forgetting**: hot → cold (evicted from the index) → frozen (quarterly epoch summaries plus a full archive bundle, disk use down to roughly a fifth) → purge (manual only). Compresses access structure, not information — the original text inside the bundle stays complete, and `thaw` restores it anytime
- **Adaptive granularity**: fragmented events get grouped, coarse events get virtual sub-segments, all done at the index layer
- **Sub-agent attribution**: delegated work is recorded as a delegation event, symmetric across both hosts
- **Cross-project promotion**: after clearing both a portability check and sensitive-data scrubbing, a lesson can be promoted to the user level
- **CLAUDE.md suggestions**: a lesson confirmed repeatedly generates a paste-ready suggestion; it never edits your docs automatically
- **Sensitive-data scrubbing**: seven classes of secret patterns are intercepted before anything gets written
- **Evaluation instrumentation**: surfacing, injection, and prefetch are logged locally as jsonl throughout the pipeline; `eventmem stats` reports the metrics in one command

## Status

v0.2. 341 tests on the Python side (including real-LLM integration and archive fault injection), 134 on the dsh plugin (including cross-language interop); end-to-end rehearsals cover the complete lifecycle from event creation through freezing and thawing. Known limitations and open questions are in [DESIGN.md §8](DESIGN.md).

`.memory/` is human-readable markdown — a glass box with zero maintenance obligation: look whenever you want, and it keeps working when you don't.
