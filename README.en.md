# MemoryPalace 1.0

[中文](README.md) · **English** · [日本語](README.ja.md)

MemoryPalace is a single-user memory system for tool collaboration, companionship and knowledge. One Python core manages sources, facts, experiences, relationships, commitments, knowledge chunks and continuity state. You can use it through plugins, the CLI, console, HTTP, MCP or SDKs.

![MemoryPalace architecture](docs/diagrams/overview.png)

## Capabilities

- **Ingestion:** The system saves hashed source snapshots and writes data with deduplication, transactions and revision checks. Persistent jobs track processing progress. If a model request fails, the saved sources and progress remain available.
- **Memory types:** Store episodes, facts and states, procedures, relationships, shared experiences, commitments and reminders, diaries, self narratives, knowledge and checkpoints. Scope each record by project, persona, collection and real or fictional world.
- **Recall:** Combine and rank candidates from exact cues, FTS5, LanceDB vectors, visual vectors and relations. The service supports historical reads, checks results against authoritative records and selects content within the context budget, with deduplication across hosts.
- **Background organization:** Generate extraction and conflict proposals, run incremental Leiden community detection and manage topic families and narrative volumes. Summaries, diaries and portraits cite their sources. Organization results support revision and rollback.
- **Files and media:** Import PDF, DOCX, PPTX, XLSX, Markdown, HTML, CSV, images, audio and video. Records retain references to source pages, paragraphs, tables, timestamps and attachments.
- **Active contact:** Configure reminders, commitment follow-ups, anniversaries, check-ins and greetings by role, with timezone, quiet hours, frequency and confirmation settings. Snooze or cancel pending messages and review the saved delivery history.
- **Console:** Check processing status, browse records with virtual scrolling, trace sources and compare revisions. The console also has a timeline, calendar, 2D/3D topic views, knowledge and attachment browsing, diaries, a recall lab, contact settings and data maintenance.

Model-generated content is labeled as such. Inferences, explicit user statements and observed operations are recorded with distinct authority. Citing the same source repeatedly does not add independent evidence.

## Install and start

Install from the GitHub repository. This release does not automatically publish packages to PyPI or npm. You need Python 3.11–3.13, Node.js 22 and [uv](https://docs.astral.sh/uv/):

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

The console and service run at `http://127.0.0.1:8319` by default, and private data is stored in `~/.memorypalace`. Use `eventmem serve` to start the service and `--root /private/path` to select an isolated data directory. The service checks local credentials, Host and Origin. Real memories, attachments, credentials and runtime logs stay outside the repository.

In the console, assign models to extraction, conflict, summary, rerank, embedding, vision, ASR and other roles. Multiple roles can share a model through a compatible remote or local endpoint. Reference credentials by environment variable name. When a role has no model configured, its tasks show a waiting status and the original source remains saved.

## Integrations and examples

| Interface | Capability |
|---|---|
| Claude Code plugin | Collect messages and tool records; handle startup recovery, pre-action recall, compaction and exit |
| `dsh-eventmem` | Send DeepSeek Harness events to the common service by default; enable legacy mode explicitly to roll back |
| HTTP `/v1` | Sources, memories, corrections, relations, continuity, jobs, maintenance, scheduling and observability |
| MCP | stdio and Streamable HTTP tool access |
| Python / TypeScript SDK | Share a contract generated from OpenAPI and provide callback deduplication interfaces |
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

Acceptance machine: **10 CPU cores, 64 GiB RAM, SSD, macOS arm64, Python 3.13.14**. The fixture contains 100,000 memories and 1,000,000 1,024-dimensional knowledge vectors with corresponding SQLite records. Vectors use a seeded 256-component Gaussian mixture and independent query samples.

| Metric | Measured |
|---|---:|
| Local fast recall p95 / p99 | 20.01 / 20.74 ms |
| Million-vector query p95 / p99 | 39.12 / 41.29 ms |
| ANN Recall@20 | 0.9985 |
| Context assembly including recall p95 | 18.76 ms |
| Recall during background import p95 | 30.32 ms |
| Peak RSS including construction | 2.75 GiB |
| Concurrent deduplication | 10 sessions, 400 requests, 200 unique sources |

The latency measurements exclude external model requests; quality on real corpora still needs separate evaluation. The [conditions, raw results and reproduction guide](docs/performance.md) reports first queries, common-term queries, background ingestion, model requests and replay separately. The functional comparison with SCARLETT is based only on its architecture diagram. There is no measured performance comparison or claim of superiority.

## Migration and retention

```sh
uv run eventmem migrate /old/project/.memory --root /isolated/memorypalace
uv run eventmem backup /private/backup.tar.gz --root /isolated/memorypalace
uv run eventmem restore /private/backup.tar.gz --root /another/empty/root
```

Migration preserves original ids, content, archives, revision links and provenance, then validates the data in an isolated target. It does not overwrite the old database. Missing external sources are labeled. Archiving preserves history; permanent deletion handles sources and their derived dependencies. Manage exported files and backups separately.

## Documentation and limits

- [Architecture and semantics](docs/architecture.md) · [Coverage matrix](docs/coverage.md) · [Configuration, plugins and operations](docs/operations.md)
- Diagrams: [write/correction](docs/diagrams/write-correct.svg), [recall/context](docs/diagrams/recall-context.svg), [background organization](docs/diagrams/background.svg), [active contact](docs/diagrams/proactive-contact.svg).
- Every diagram has [editable Mermaid, SVG and PNG files](docs/diagrams/). CI checks the [OpenAPI contract](contracts/openapi.json), SDKs, plugins, console, media processing and diagram generation.

Fast lexical recall filters by scope and ranks up to 400 of the most recent matches. Deep mode supports ranking the full match set and optional model retrieval.

PDF parsing uses native text by default. Scanned pages need a vision endpoint; full local layout models are optional. Visual retrieval requires a compatible multimodal embedding endpoint. External models, heavy parsing and differences between corpora affect end-to-end latency and quality.

MemoryPalace runs locally on macOS/Linux for a single user. This release does not include multi-user accounts, live camera/microphone capture or dedicated chat-platform clients.

[MIT License](LICENSE)
