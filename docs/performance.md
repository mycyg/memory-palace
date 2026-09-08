# Measurements and reproducibility

## Acceptance environment

The release was measured on **10 CPU cores, 64 GiB RAM, SSD, macOS 26.6.1 arm64, Python 3.13.14**. This recorded machine is the acceptance environment. These measurements do not establish performance on an 8-core / 32 GiB machine or on another operating system.

The full fixture contains **100,000 memories and 1,000,000 knowledge records with 1,024-dimensional vectors**. Vectors use a fixed seed (60260908), 256 Gaussian components and sigma 0.012. Query vectors are independent held-out samples; Recall@20 is compared with exhaustive inner-product search over the same normalized vectors. This synthetic distribution measures index behavior and storage scale; it does not measure semantic relevance on personal documents. See the [complete raw scale report](benchmarks/scale-macos-arm64.json) and [query workload report](benchmarks/query-workloads.json).

## Scale and latency

| Measurement | Samples | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Local recall, 100 identifiers repeated three times | 300 | 3.04 ms | 20.01 ms | 20.74 ms |
| ANN, first pass | 100 | 36.08 ms | 44.63 ms | 126.00 ms |
| ANN, warm queries | 100 | 36.31 ms | 39.12 ms | 41.29 ms |
| Context assembly including retrieval | 300 | 2.09 ms | 18.76 ms | 19.38 ms |
| Recall during background receipt of 1,000 sources | 100 | 21.81 ms | 30.32 ms | 46.80 ms |
| Distinct identifier queries | 100 | 19.38 ms | 26.90 ms | 59.44 ms |
| Distinct topic queries | 100 | 18.00 ms | 20.34 ms | 26.28 ms |
| Common terms, five queries repeated 20 times | 100 | 6.56 ms | 10.68 ms | 14.90 ms |
| Startup context | 100 | 6.53 ms | 7.20 ms | 12.07 ms |
| HTTP loopback, authenticated recall | 100 | 9.30 ms | 10.41 ms | 13.04 ms |

The first local query in the full run took **95.43 ms**. Five separate Python processes took **84.90–94.36 ms** to initialize the core and finish their first recall. The operating-system filesystem cache was **not flushed**; these are fresh-process measurements, not disk-cold guarantees. HTTP measurements use loopback and include request handling, canonical filtering and response serialization. Separate workloads use different queries, so their latency distributions are not additive.

| Measurement | Result | Acceptance threshold |
|---|---:|---:|
| ANN Recall@20 | 0.9985 | ≥0.95 |
| Local warm recall p95 / p99 | 20.01 / 20.74 ms | ≤50 / 150 ms |
| ANN warm p95 / p99 | 39.12 / 41.29 ms | ≤250 / 750 ms |
| Context assembly p95 | 18.76 ms | ≤100 ms |
| Peak process RSS, including fixture construction | 2.75 GiB | ≤8 GiB |
| Concurrent source receipt | 400 requests / 200 unique sources / 10 sessions | No loss or duplicate commit |
| Receipt throughput for that concurrent workload | 1,206 requests/s | Reported |
| Bulk fixture construction | 60.66 s | Reported |
| IVF_HNSW_SQ build | 195.92 s | Reported |

The fast FTS path ranks at most 400 recent matches per allowed scope, then combines those candidates with exact, precomputed, relation and available vector candidates. This bounds common-term latency and can omit older matches; `mode: "deep"` uses the complete FTS match set. Every returned candidate is checked against SQLite scope, revision and effective state. The measured local path requires no generative model. Budget assertions check the actual assembled text using the configured tokenizer.

The background workload covers reliable source receipt and index invalidation. Heavy document parsing and model processing are separately scheduled and are not part of that import latency result. External services and media parser process memory are not included in core RSS.

## Temporal and answer replay

The [raw configured-model replay](benchmarks/model-replay.json) includes eight synthetic regression cases: two tool, three companion and three knowledge cases. All six baselines use the same question, 2,000-token limit, answer model and judge model. Sources and revisions available after the cutoff are excluded. The model configuration fingerprints are identical across answer and judge roles; endpoint addresses, credentials and personal memories are absent from the report.

| Baseline | Tool task success | Companion task success | Knowledge task success |
|---|---:|---:|---:|
| No memory | 0/2 | 1/3 | 1/3 |
| Recent extractive summary | 1/2 | 1/3 | 2/3 |
| BM25 | 1/2 | 1/3 | 1/3 |
| Deterministic hashed-vector fixture | 1/2 | 1/3 | 2/3 |
| MemoryPalace at `e66710c` | 1/2 | 1/3 | 2/3 |
| MemoryPalace 1.0 | 2/2 | 3/3 | 3/3 |

The legacy adapter executes the historical repository code. The vector baseline uses deterministic 256-dimensional token hashing, **not a learned semantic embedding model**. The recent-summary baseline uses the first sentence of each of ten recent records. These deliberately small baselines test correction and temporal semantics; they do not establish broad model or retrieval superiority.

MemoryPalace 1.0 returned zero cross-scope or stale records in these cases. The configured judge marked zero unsupported answers and zero stale-answer misuse for 1.0. Retrieval recall was 1.00 for tool and companion and **0.67 for knowledge**: the abstention case returned evidence stating that the document did not contain an answer, while its retrieval target set was empty. The answer correctly abstained. The raw metric is retained.

| MemoryPalace 1.0 scenario | Mean context tokens | Mean model answer latency |
|---|---:|---:|
| Tool | 28.5 | 1,540.6 ms |
| Companion | 21.0 | 1,322.1 ms |
| Knowledge | 40.0 | 9,497.0 ms |

Answer latency includes the answer provider call and its retries; it excludes judge calls. The long knowledge latency remains in the result. A separate source receipt took 4.01 ms and completed extraction in **56.10 seconds**, including queued processing and model retries. This is an end-to-end model pipeline observation, not a local recall measurement.

All 48 baseline cells completed across resumable runs. Invalid structured responses caused retries and process restarts. The final process reported 50,829 model tokens, including extraction and the nine remaining baseline cells; this is **not total usage across earlier resumed runs**. Failed calls without returned usage cannot be counted. Provider prices were not supplied, so no currency cost is asserted. Automatic judging on eight cases has substantial uncertainty; user-specific historical replay and a configured semantic embedding baseline remain necessary for a broader quality assessment.

An additional [single-source extraction probe](benchmarks/model-ingestion.json) used 580 returned model tokens and completed receipt plus extraction in 4.26 seconds (receipt: 3.90 ms). Linear normalization is 580,000 tokens per 1,000 equivalent sources; only **one source** was measured. Conflict and embedding roles were unconfigured in this probe, so their follow-on tasks remained waiting for configuration and their costs are excluded. A longer five-source attempt with conflict processing was interrupted before a complete report and supplies no throughput result.

## Correctness and product checks

Local checks passed: **382 Python tests** (three optional legacy real-model tests deselected), **136 DeepSeek Harness tests**, **3 TypeScript SDK tests**, and **5 browser workflows**. The separate configured-model replay above exercises real model requests.

Tests exercise revision compare-and-swap, source deduplication, SQLite contention, lease expiry, process termination after a remote callback effect, cancellation, model failure, stale vector revisions, deletion and rebuild, historical queries, role/project separation, source citation, document versions and shared context budgets. A remote effect followed by process exit leaves a durable `sending` entry; an unacknowledged non-idempotent delivery becomes `uncertain` on restart. Acknowledgment reconciles that occurrence without resuming canceled work.

The protocol suite executes HTTP, Python SDK, TypeScript SDK, CLI, MCP stdio, MCP Streamable HTTP and Claude host events against synthetic stores. DeepSeek Harness tests cover its service adapter and compatible legacy behavior. Browser workflows cover import, correction, provenance, archive/restore, graph and recall views, mobile layout, backup download, permanent-deletion preview, and schedule pause/snooze/cancel. Native PDF and actual FFmpeg decoding are exercised; OCR, vision and ASR model responses in automated tests are controlled fixtures. No claim of measured media-model accuracy is made.

SCARLETT has [diagram coverage only](coverage.md). There is no runnable implementation for a valid performance comparison, and no SCARLETT scores are inferred.

## Reproduce

Use a full checkout, install the locked dependencies and build the SDK and console as described in [operations](operations.md). Use empty synthetic roots for scale runs:

```sh
uv run pytest -q
npm test --prefix dsh-plugin
npm test --prefix sdk/typescript
npm test --prefix console
uv run eventmem evaluate --output /tmp/memorypalace-replay.json
uv run eventmem benchmark --scale full --root /tmp/memorypalace-scale --output /tmp/memorypalace-scale.json
uv run python scripts/benchmark_queries.py --root /tmp/memorypalace-scale --output /tmp/memorypalace-queries.json
```

Configure answer, judge, extraction and conflict model roles in an isolated root, with API secrets supplied through environment variables, then run:

```sh
uv run python scripts/model_replay.py --root /private/model-config --output /tmp/model-replay.json
uv run python scripts/model_ingestion.py --root /private/model-config --output /tmp/model-ingestion.json --samples 5
```

The model scripts copy only model configuration into a temporary synthetic store. They never replay production memories. Results depend on the configured service; keep fingerprints, data, model, budget and cutoffs fixed when comparing runs. The independently invocable scale CI workflow records its own hardware; routine GitHub runners are not equivalent to the measured Mac.
