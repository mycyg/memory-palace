# MemoryPalace 1.0

[中文](README.md) · **English** · [日本語](README.ja.md)

MemoryPalace is a single-user memory system for tool collaboration, companionship and knowledge. It stores sources, facts, experiences, relationships, commitments, knowledge chunks and continuity state. You can use it through plugins, the CLI, console, HTTP, MCP or SDKs.

![MemoryPalace architecture](docs/diagrams/overview.png)

## Capabilities

- **Ingestion:** Save original sources, deduplicate incoming data and track processing progress. If a model request fails, the saved sources and progress remain available.
- **Memory types:** Store episodes, facts and states, procedures, relationships, shared experiences, commitments and reminders, diaries, self narratives, knowledge and checkpoints. Scope each record by project, persona, collection and real or fictional world.
- **Recall:** Find memories by exact cues, full text, meaning, images or relations, including records from a past point in time. Results reflect corrections, fit the context budget and are deduplicated across hosts.
- **Background organization:** Extract memories from sources, propose resolutions for conflicts and organize topic families and narrative volumes. Summaries, diaries and portraits cite their sources. Organization results support revision and rollback.
- **Files and media:** Import PDF, DOCX, PPTX, XLSX, Markdown, HTML, CSV, images, audio and video. Records retain references to source pages, paragraphs, tables, timestamps and attachments.
- **Active contact:** Configure reminders, commitment follow-ups, anniversaries, check-ins and greetings by role, with timezone, quiet hours, frequency and confirmation settings. Snooze or cancel pending messages. Pending messages and delivery history survive restarts.
- **Console:** Check processing status, browse records, trace sources and compare revisions. The console also has a timeline, calendar, 2D/3D topic views, knowledge and attachment browsing, diaries, a recall lab, contact settings and data maintenance.

Model-generated content is labeled as such. Inferences, explicit user statements and observed operations are recorded with distinct authority. Citing the same source repeatedly does not add independent evidence.

### Writing and corrections

![Writing and corrections](docs/diagrams/write-correct.png)

### Recall and context

![Recall and context](docs/diagrams/recall-context.png)

### Background organization

![Background organization](docs/diagrams/background.png)

### Active contact

![Active contact](docs/diagrams/proactive-contact.png)

## Install and start

You need Python 3.11–3.13, Node.js 22 and [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen --extra all --extra dev
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm ci --prefix console
npm run build --prefix console
uv run eventmem console
```

The console and service run at `http://127.0.0.1:8319` by default, and private data is stored in `~/.memorypalace`. Use `eventmem serve` to start the service and `--root /private/path` to select an isolated data directory.

In the console, assign models to extraction, conflict, summary, rerank, embedding, vision, ASR and other roles. Multiple roles can share a model through a compatible remote or local endpoint. Reference credentials by environment variable name. When a role has no model configured, its tasks show a waiting status and the original source remains saved.

## Integrations and examples

| Interface | Capability |
|---|---|
| Claude Code plugin | Collect messages and tool records; handle startup recovery, pre-action recall, compaction and exit |
| `dsh-eventmem` | Send DeepSeek Harness events to the common service by default; enable legacy mode explicitly to roll back |
| HTTP `/v1` | Sources, memories, corrections, relations, continuity, jobs, maintenance, scheduling and observability |
| MCP | stdio and Streamable HTTP tool access |
| Python / TypeScript SDK | Call the memory service and deduplicate callbacks |
| `eventmem` CLI | Service, console, MCP, ingestion/recall, migration, backup, scheduling and evaluation |

MCP provides tool access. Automatic collection and passive context injection require a host event adapter. Keep the local service running when using plugins.

```sh
uv run python examples/v1/scenarios.py tool
uv run python examples/v1/scenarios.py companion
uv run python examples/v1/scenarios.py knowledge
node examples/v1/tool.mjs
```

[Runnable examples](examples/v1/) cover tool work, shared experiences and commitments, document import and a contact callback. Until a sending policy and channel are configured, the system generates suggestions only. Callbacks use stable delivery ids for deduplication. Channels without idempotency support show uncertain delivery states.

## Measured performance

Test environment: **10 CPU cores, 64 GiB RAM, SSD, macOS arm64, Python 3.13.14**. The dataset contains 100,000 memories and 1,000,000 synthetic 1,024-dimensional knowledge vectors, with independent query samples.

| Metric | Measured |
|---|---:|
| Local fast recall p95 / p99 | 20.01 / 20.74 ms |
| Million-vector query p95 / p99 | 39.12 / 41.29 ms |
| ANN Recall@20 | 0.9985 |
| Context assembly including recall p95 | 18.76 ms |
| Recall during background import p95 | 30.32 ms |
| Peak RSS including construction | 2.75 GiB |
| Concurrent deduplication | 10 sessions, 400 requests, 200 unique sources |

The latency measurements exclude external model requests; quality on real corpora still needs separate evaluation. [Benchmark details](docs/performance.md).

## Migration and retention

```sh
uv run eventmem migrate /old/project/.memory --root /isolated/memorypalace
uv run eventmem backup /private/backup.tar.gz --root /isolated/memorypalace
uv run eventmem restore /private/backup.tar.gz --root /another/empty/root
```

Migration preserves original ids, content, archives, revision links and provenance, then validates the data in an isolated target. It does not overwrite the old database. Missing external sources are labeled. Archiving preserves history; permanent deletion handles sources and their derived dependencies. Manage exported files and backups separately.

## Documentation and limits

[Architecture and semantics](docs/architecture.md) · [Configuration, plugins and operations](docs/operations.md)

Fast lexical recall filters by scope and ranks up to 400 of the most recent matches. Deep mode supports ranking the full match set and optional model retrieval.

PDF parsing uses native text by default. Scanned pages need a vision endpoint; full local layout models are optional. Visual retrieval requires a compatible multimodal embedding endpoint. External models, heavy parsing and differences between corpora affect end-to-end latency and quality.

MemoryPalace runs locally on macOS/Linux for a single user.

[MIT License](LICENSE)
