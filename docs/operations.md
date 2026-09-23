# Installation and operations

## Install and run

MemoryPalace 2.0 requires Python 3.10 or newer. The basic `eventmem` installation uses SQLite and does not require a model, a private configuration file, or Node.js at runtime. A built wheel includes the browser console. Building the console from a Git checkout requires Node.js 22 and npm.

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen
uv run eventmem serve --root /private/work-memory
```

The service listens on `127.0.0.1:8319`; run `eventmem console --root /private/work-memory` instead of `serve` to start it and open its browser UI. `EVENTMEM_HOME` changes the default data root. Use a separate root for each isolated dataset. The root contains SQLite data, source snapshots, a local service token, and generated files; treat it as private.

The CLI can receive and recall without a running server. HTTP, SDK, and service-based plugins need the server. `eventmem mcp --root /private/work-memory` starts a stdio MCP server. Streamable HTTP MCP is available from the running service. The Python and TypeScript SDKs are generated from the current [OpenAPI contract](../contracts/openapi.json).

## Optional dependencies and models

Install only the extras you use: `vector` for vector storage, `graph` for clustering, `media` for richer file parsing, or `local-embedding` for an optional local embedding service. `all` combines vector, graph, and media. The local model is opt-in and may download weights; see [local embeddings](local-embedding.md).

Model roles can be configured in the console or through `PUT /v1/settings/models`. This endpoint replaces the role map, so retain existing roles when updating it. Keys are supplied through environment variables named by `api_key_env`, not embedded in the role configuration. No endpoint is mandatory for source receipt, corrections, checkpoints, or lexical recall. A job that needs an unconfigured role remains visible as waiting for configuration.

## Host integrations

Codex, Claude Code, and DeepSeek Harness plugins call the same local service. They use stable source identities and a local offline spool for observations. They do not maintain a legacy file-store runtime or another memory engine. The host controls whether and when context is injected; explicit MCP calls remain available. See the [Codex guide](codex.md) and each plugin's own README for installation.

Preparing context reserves capacity without counting it as received. DeepSeek Harness confirms the exact plugin message only when it enters an active native model step; queueing alone leaves it unconfirmed. An exact native discard, or a claimed message whose turn ends without entering the model, releases that reservation through a `discarded` receipt. This receipt flow establishes request inclusion or rejection, not model understanding or task completion.

A background attempt shares one deadline across model requests and checks it again before committing. Local preparation retains its execution slot until it returns, including work that cannot be interrupted. A transport timeout does not prove that a remote provider stopped processing the request.

An HTTP client sends the local bearer token from `<root>/local-token`. The service defaults to loopback and does not provide multi-user authorization. Keep the token and root private. A plugin's accepted spool entry is an observation queued for receipt, not proof that the service stored it until a receipt is returned.

## Upgrade and recovery

Version 2.0 changes some 1.x public interfaces. Back up the old root first and migrate into a distinct empty target:

```sh
eventmem migrate /old/store --root /private/work-memory-v2 --scope '{"project":"work"}'
eventmem backup /private/backups/work-memory.tar.gz --root /private/work-memory-v2
eventmem restore /private/backups/work-memory.tar.gz --root /another/empty/root
eventmem export /private/exports/work-memory.jsonl --root /private/work-memory-v2
```

`migrate` accepts a 1.x `memory.sqlite3` file or its containing directory, and the older `.memory` file store. The optional `--scope` assigns a scope only to historical file stores, which did not contain one; SQLite migration preserves every original row scope. Migration reads the old store without modifying it and publishes the target only after validation. Source snapshots and known revision history are preserved where the source format contains them. Derived indexes are rebuilt in the target. Running jobs are made retryable. Unfinished 1.x summary jobs with eligible source records become event summary jobs; those without eligible records complete without a model call. Unfinished diary, portrait, self-narrative, and prediction jobs are preserved as canceled, with a retirement error and per-kind counts in `migration-report.json`. An in-flight external callback becomes uncertain and requires reconciliation before another send. External conversation pointers remain unverified until their source is separately available. Inspect the migration report and compare scoped recall before selecting the new root.

Legacy `diary`, `portrait`, `self_narrative`, and `prediction` records remain available for historical or audit reads. Version 2.0 rejects new records of those retired kinds.

A backup contains the SQLite snapshot and only the attachments it references. Restore verifies the database and attachment checksums in isolated staging, prepares recovery and index rebuild, then publishes into an empty root. A failed restore leaves that target available for retry. Exported or downloaded files are independent copies of the data and are not removed by a record deletion. Permanent deletion follows source and derived-record dependencies in the active store; retain an isolated backup if you need a reversible recovery path.

## Reminders and callbacks

A reminder is an explicit schedule tied to an existing record and policy. The default policy does not send. A worker in `eventmem serve` evaluates due schedules; a configured callback can receive delivery attempts. The host owns recipients, authentication, and actual channel behavior. See [work reminders](reminders.md) for the request shape and callback idempotency rules.

## Validate a checkout

```sh
uv sync --frozen --extra dev
uv run pytest -q
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm test --prefix sdk/typescript
npm ci --prefix dsh-plugin
npm run typecheck --prefix dsh-plugin
npm test --prefix dsh-plugin
npm run build --prefix dsh-plugin
npm ci --prefix console
npm run build --prefix console
uv run python scripts/generate_contract.py
npm run generate --prefix sdk/typescript
git diff --exit-code -- contracts src/eventmem/sdk sdk/typescript/src
node scripts/check-docs.mjs
uv build
uv run python scripts/check_wheel.py
```

The wheel check verifies the console assets, CLI entry point, typed SDK, and absence of private/runtime files. Browser tests additionally need Playwright Chromium. The manual synthetic benchmark uses one temporary, model-free work corpus and writes its raw observations:

```sh
uv run python scripts/benchmark_work_memory.py --output /tmp/work-memory-benchmark.json
```

This benchmark reports recall hits, token counts, and local timing without a pass/fail latency threshold. Its process cache is cleared for one measurement, but the operating-system filesystem cache is not. See the [2.0 synthetic report and raw data](benchmarks/work-memory-v2.md). The [1.x results](performance.md) use different fixtures.
