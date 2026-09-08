# MemoryPalace 1.0

[中文](README.md) · **English** · [日本語](README.ja.md)

A single-user memory system for tool collaboration, companionship and knowledge. Sources, facts, experiences, relationships, commitments, knowledge chunks and continuity state share one Python core, accessed through plugins, CLI, console, HTTP, MCP and SDKs.

![MemoryPalace architecture](docs/diagrams/overview.png)

## Capabilities

- **Durable ingestion:** hashed source snapshots, deduplication, transactions, revision checks and persistent jobs. Model failures retain sources and processing progress.
- **Scenario memory:** episodes, facts/states, procedures, relationships, shared experiences, commitments/reminders, diaries, self narratives, knowledge and checkpoints. Project, persona, collection and real/fictional world are separate scope fields.
- **Unified recall:** exact cues, FTS5, LanceDB vectors, visual vectors and relations; fusion, historical reads, canonical validity checks, context budgets and cross-host deduplication.
- **Background organization:** extraction/conflict proposals, incremental Leiden communities, topic families and narrative volumes, cited summaries/diaries/portraits, revision and rollback.
- **Multimodal sources:** PDF, DOCX, PPTX, XLSX, Markdown, HTML, CSV, images, audio and video with page, paragraph, table, timestamp and attachment provenance.
- **Active contact:** role-specific reminders, commitment follow-ups, anniversaries, check-ins and greetings with timezone, quiet hours, frequency, confirmation, snooze, cancellation and durable delivery history.
- **Console:** processing overview, virtualized browsing, provenance, revision comparison, timeline/calendar, 2D/3D topics, knowledge/attachments, diaries, recall lab, contact and maintenance.

Generated content remains labeled. Inferences, explicit user statements and observed operations retain distinct authority; repeated citations do not create independent evidence.

## Install and start

This release is delivered through GitHub; packages are not automatically published to PyPI or npm. Use Python 3.11–3.13, Node.js 22 and [uv](https://docs.astral.sh/uv/):

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

The console and service use `http://127.0.0.1:8319`; private data defaults to `~/.memorypalace`. Use `eventmem serve` for the service or `--root /private/path` for an isolated database. Local credentials, Host and Origin checks protect the service. Real memories, attachments, credentials and runtime logs stay outside the repository.

Configure model roles in the console: extraction, conflict, summary, rerank, embedding, vision, ASR and others can share compatible remote or local endpoints. Credentials are environment-variable references. Unconfigured roles leave visible waiting tasks while retaining the original source.

## Integrations and examples

| Interface | Capability |
|---|---|
| Claude Code plugin | Message/tool collection, startup recovery, pre-action recall, compaction and exit |
| `dsh-eventmem` | DeepSeek Harness events using the common service by default; explicit legacy mode for rollback |
| HTTP `/v1` | Sources, memories, corrections, relations, continuity, jobs, maintenance, scheduling and observability |
| MCP | stdio and Streamable HTTP tool access |
| Python / TypeScript SDK | Contract generated from the same OpenAPI, plus callback deduplication interfaces |
| `eventmem` CLI | Service, console, MCP, ingestion/recall, migration, backup, scheduling and evaluation |

MCP tool access does not automatically collect or passively inject host context; a host event adapter provides those behaviors. Keep the local service running when using plugins.

```sh
uv run python examples/v1/scenarios.py tool
uv run python examples/v1/scenarios.py companion
uv run python examples/v1/scenarios.py knowledge
node examples/v1/tool.mjs
```

[Runnable examples](examples/v1/) cover tool work, shared experiences/commitments, document import and a contact callback. Unconfigured sending policies/channels produce suggestions. Stable delivery ids support deduplication; non-idempotent channels expose uncertain delivery states.

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

These measurements exclude external model latency and do not guarantee quality on real corpora. First query, common terms, background ingestion, model requests and replay are reported separately in the [conditions, raw results and reproduction guide](docs/performance.md). SCARLETT has diagram-only functional coverage; no competitor timing or superiority claim is inferred.

## Migration and retention

```sh
uv run eventmem migrate /old/project/.memory --root /isolated/memorypalace
uv run eventmem backup /private/backup.tar.gz --root /isolated/memorypalace
uv run eventmem restore /private/backup.tar.gz --root /another/empty/root
```

Migration preserves original ids, content, archives, revision links and provenance in an isolated target without overwriting the old database. Missing external sources remain labeled. Archive retains history; permanent deletion handles source and derived dependencies. Exported files and backups require separate retention management.

## Documentation and limits

- [Architecture and semantics](docs/architecture.md) · [Coverage matrix](docs/coverage.md) · [Configuration, plugins and operations](docs/operations.md)
- Diagrams: [write/correction](docs/diagrams/write-correct.svg), [recall/context](docs/diagrams/recall-context.svg), [background organization](docs/diagrams/background.svg), [active contact](docs/diagrams/proactive-contact.svg).
- [Editable Mermaid, SVG and PNG](docs/diagrams/) accompany every diagram. CI covers the [OpenAPI contract](contracts/openapi.json), SDKs, plugins, console, media and diagram generation.

Fast lexical recall ranks up to 400 recent scoped matches; deep mode supports the full match set and optional model retrieval. Native PDF text parsing is the default; scanned pages need a vision endpoint and full local layout models are optional. Visual retrieval requires a compatible multimodal embedding endpoint. External models, heavy parsing and corpus differences affect end-to-end latency and quality. Deployment is single-user macOS/Linux; multi-user accounts, live camera/microphone capture and dedicated chat-platform clients are outside the product scope.

[MIT License](LICENSE)
