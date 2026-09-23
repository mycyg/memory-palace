# Synthetic work-memory retrieval · 2026-09-23

This is a local, model-free comparison of the 1.x source tree at commit `fa5dce37432041b20df416a90f99ce81889fd753` and the MemoryPalace 2.0 candidate in this change set. Both runs used the exact same [benchmark script](../../scripts/benchmark_work_memory.py), corpus, query order, token budget, and machine. The raw reports are [before](work-memory-v2-before.json) and [after](work-memory-v2-after.json).

The fixture creates 240 synthetic work records with fixed source identities and timestamps. It asks 40 distinct exact-cue queries, limits each response to eight records and 800 tokens, and blocks model calls. Each query is measured once after clearing the process recall cache and once immediately afterward. The operating-system filesystem cache was not flushed.

| Measurement | 1.x source | 2.0 candidate |
|---|---:|---:|
| Target record present in top eight | 40/40 | 40/40 |
| Model calls | 0 | 0 |
| Context tokens, mean / maximum | 386.275 / 392 | 386.275 / 392 |
| Cache-cleared recall, median / p95 | 10.46 / 11.87 ms | 12.47 / 16.02 ms |
| Warm recall, median / p95 | 4.65 / 5.20 ms | 5.81 / 6.74 ms |

The 2.0 timing measurements are higher in these single runs. The returned record IDs and per-query token counts match between the two runs. This fixture checks local retrieval of exact synthetic cues and context bounds; it does not measure real-document semantic relevance, answer quality, model latency, or external provider behavior. The run environment was macOS arm64, Python 3.14.6, and SQLite 3.53.1. Timing on another machine or under another load may differ.

To rerun the 2.0 fixture from the repository root:

```sh
uv sync --frozen
uv run python scripts/benchmark_work_memory.py --output /tmp/work-memory-v2.json
```

For a direct comparison, run the same script with the `src` tree from the recorded 1.x commit in a separate temporary checkout, then with the 2.0 source. Keep the Python environment, machine, corpus, and query order the same. The script writes per-query timing, hit flags, returned IDs, and token counts, so the aggregate table can be checked against the raw observations. The older [1.x performance report](../performance.md) used other fixtures and is not directly comparable to these runs.
