# Local embeddings and channel memory repair

## Qwen3-Embedding-0.6B

Install the optional local inference dependencies in the same Python environment as the memory service:

```sh
uv sync --frozen --extra all --extra dev --extra local-embedding
```

Add this role to the existing model configuration. Preserve other roles when writing `/v1/settings/models`, because that endpoint replaces the configuration map.

```json
{
  "embedding": {
    "endpoint": "http://127.0.0.1:8321/v1",
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "protocol": "openai",
    "dimensions": 1024,
    "preprocessing": "qwen3-97b0c614-last-token-8192-normalized-v1",
    "timeout_seconds": 600,
    "local_embedding": true
  }
}
```

The opt-in `local_embedding` flag starts a loopback-only service on demand. A file lock serializes process startup across hosts; the service checks a private token stored under the memory root. An occupied port belonging to another service is an error. Remote endpoints cannot invoke this launcher.

The model is pinned to revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`. A complete cached snapshot loads without a network request. Model download occurs through Hugging Face if the snapshot is absent or incomplete. The process starts before loading weights; the first embedding request loads the model. Apple Silicon uses PyTorch MPS when available; other machines use CPU. Inference is serialized, uses normalized vectors, and truncates each input to 8,192 tokens. Large documents should be chunked before embedding. There is no idle-unload policy; the process retains the loaded model.

A dropped connection during an embedding request triggers one service recovery and one retry. HTTP 503 triggers one inference retry; the server discards a failed model instance so it can reload. Embeddings are idempotent. A second failure propagates into the existing durable job retry policy, and no vector is committed as a successful result. Read timeouts follow that durable retry policy rather than killing a potentially working process. This retry behavior does not apply to chat generation or external side effects.

`/health` reports process availability and whether the model has loaded. Only a successful `/v1/embeddings` response demonstrates completed inference. The endpoint does not report model token usage, so its token counter must not be interpreted as measured inference consumption.

Changing model revision, truncation or normalization requires a new `preprocessing` identity and rebuilding vectors. Deep retrieval uses configured embeddings; the normal fast path remains lexical and relational. This endpoint currently uses unprompted embeddings for both documents and queries; task-specific query prompting is a separate retrieval change requiring evaluation.

[Official model instructions](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/README.md).

## Transport context and historical records

Recognized channel envelopes separate the current user message from repeated host delivery history. Host ingestion preserves the full envelope in an immutable source snapshot, while extraction and passive message recall use the current body. Malformed envelopes and ordinary quotations remain unchanged. Parsing an envelope does not grant its contents instruction authority. Extraction retries exclude archived records and prior generated proposals; completed media annotations remain eligible evidence. Legacy queued extraction chunks cannot produce claims supported only by excluded transport history.

Empty tool cues do not request unrelated memories. Companion passive recall excludes raw tool events; explicit searches and reads retain access to the operation evidence. Graph neighbors have a lower retrieval weight and seeds are not counted again through their own relation edges.

Existing contaminated records can be reviewed with:

```sh
uv run python scripts/repair_channel_envelopes.py \
  --root /private/memory-root \
  --scope '{"project":"personal","persona":"companion","collection":"default","world":"real"}' \
  --output /private/channel-repair-plan.json
```

Inspect the private plan and make a backup before adding `--apply`. The script corrects original message records and archives generated entries only when their exact quoted evidence occurs in excluded transport history and not in the current body. Multi-source entries are skipped. Source snapshots and revision history remain; records are not permanently deleted. Concurrent revision changes fail rather than silently overwrite newer content. Re-running plans the remaining current records.

Semantic preference changes require an evidence-backed `replace` revision linking the obsolete record to its replacement. The repair script does not guess that one preference replaces another.

## Operational diagnosis

The overview is explicitly store-wide. `job_details` groups failed, retrying and waiting-for-configuration tasks by kind and sanitized error. Missing embedding configuration is distinct from failed extraction. Provider HTTP status codes are retained without response bodies, credential-bearing URLs or headers. HTTP 402 requires resolving the provider account or selecting an available model; retry loops cannot repair it.
