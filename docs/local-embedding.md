# Optional local embeddings

The basic MemoryPalace installation uses SQLite lexical recall and needs no embedding model. Install `local-embedding` only if you want the optional loopback embedding service:

```sh
uv sync --frozen --extra local-embedding
```

An embedding role specifies an endpoint, model, vector dimensions, and preprocessing identity. The role can be configured through the console or `PUT /v1/settings/models`. That endpoint replaces the complete model-role map, so preserve any roles you still use. Keep credentials in environment variables named by `api_key_env`.

```json
{
  "embedding": {
    "endpoint": "http://127.0.0.1:8321/v1",
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "protocol": "openai",
    "dimensions": 1024,
    "preprocessing": "qwen3-97b0c614-last-token-8192-normalized-v1",
    "local_embedding": true
  }
}
```

This example opts into the built-in Qwen launcher. The initial model snapshot may be downloaded; later complete cached snapshots can load without a network request. A change in model revision, dimensions, normalization, or truncation should use a new preprocessing identity and rebuild derived vectors. Lexical recall remains available if embedding is unconfigured or unavailable. Vector retrieval and its quality depend on the model and corpus; the [historical 1.x measurements](performance.md) are not 2.0 results.

The local process binds to loopback and uses a private token under the memory root. A successful embedding response, rather than process startup alone, confirms that inference completed. Model loading and media parsing can consume substantial memory; install these features only for a workload that needs them.
