# MemoryPalace 2.0 · Work memory

[中文](README.md) · **English** · [日本語](README.ja.md)

MemoryPalace stores provenance-aware memory for tools and collaborative applications. It receives messages, operation results, and documents as sources; keeps revisable events and knowledge; and returns relevant context within a defined scope and token budget. The Python package and CLI are named `eventmem`.

It helps work continue across sessions: confirmed progress, unverified outcomes, commitments, procedures, and the next entry point can be retained. The host executes tasks and decides when to read memory. MemoryPalace does not run an agent or initiate user messages.

## Capabilities

- **Sources and revisions:** Stable source identities, content snapshots, citations, and revision history. Current recall rechecks corrections, retractions, and archives.
- **Work continuity:** Events, summaries, checkpoints, commitments, and procedures use the same source and record flows. Session boundaries can keep confirmed progress, unknowns, and a next step.
- **Bounded recall:** Search by project, collection, time, and token budget. Exact matching and SQLite full-text search work without a model; vectors and relations are optional.
- **Durable processing:** SQLite is the only authoritative runtime store. Background jobs, cache invalidation, and retries are persisted; indexes can be rebuilt.
- **Interfaces:** Local HTTP service, MCP, Python and TypeScript SDKs, CLI, and browser console. Codex, Claude Code, and DeepSeek Harness integrations use the service and spool observations locally when offline.
- **Optional features:** Document and media parsing, embeddings, graphs, explicit reminders, and host callbacks. Basic installation needs no model, private configuration, or external service.

## Quick start

Python 3.10 or newer is required. Repository development and console builds use Node.js 22 and [uv](https://docs.astral.sh/uv/). Installing a built wheel does not require Node.js.

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen
uv run eventmem serve --root ./example-data
```

In another terminal, receive a synthetic work record and recall it:

```sh
uv run eventmem receive --root ./example-data --json '{"namespace":"demo","key":"rollback-1","occurred_at":"2026-09-01T00:00:00Z","scope":{"project":"demo"},"kind":"procedure","authority":"operation","text":"Rollback uses the last verified artifact."}'
uv run eventmem recall --root ./example-data --json '{"query":"rollback artifact","scope":{"project":"demo"},"scenario":"tool","budget":500}'
```

`eventmem console --root ./example-data` starts the service and opens the console instead of `serve`. The service listens on `127.0.0.1:8319` by default, and the default data directory is `~/.memorypalace`; `--root` selects an isolated directory. The CLI can access a local store directly. HTTP and SDK clients require a running service.

## Boundaries

Original sources and model-generated summaries carry different evidence labels. Model settings only enable selected background features; source receipt, revisions, and local recall work without them. Reminders track plans and callback results. The host owns recipients, channel permissions, and delivery.

Prepared or queued context reserves capacity; only an exact-body `accepted` receipt counts it as received, and a confirmed discard releases the reservation. A timed-out background job retains its execution slot until its local call ends.

Version 2.0 changes public contracts and breaks some 1.x integrations. Back up existing data and migrate into a **separate empty directory**; migration leaves the original store untouched. See [installation and migration](docs/operations.md).

Backups include only attachments referenced by their SQLite snapshot. Restore checks the database and attachments in an isolated staging directory before publishing to an empty target.

[Current architecture](docs/architecture.md) · [Integration and operations](docs/operations.md) · [Work examples](examples/README.md) · [Work reminders](docs/reminders.md) · [Codex integration](docs/codex.md) · [2.0 synthetic benchmark](docs/benchmarks/work-memory-v2.md) · [Historical 1.x measurements](docs/performance.md)

[MIT License](LICENSE)
