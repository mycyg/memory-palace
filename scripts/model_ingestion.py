"""Measure a bounded synthetic extraction workload using configured model roles."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from eventmem.core import Engine, SourceInput
from eventmem.core.jobs import Worker


def run(config, output, samples=5):
    with tempfile.TemporaryDirectory() as root:
        engine = Engine(root)
        engine.settings("models", config)
        receipt = []
        ids = []
        started = time.perf_counter()
        for i in range(samples):
            start = time.perf_counter()
            source = engine.receive(
                SourceInput(
                    namespace="model-ingestion",
                    key=str(i),
                    text=f"For synthetic project {i}, the user explicitly prefers concise release notes with the validation results.",
                    extract=True,
                )
            )
            receipt.append((time.perf_counter() - start) * 1000)
            ids.append(source["id"])
        worker = Worker(engine)
        while worker.run_once():
            pass
        elapsed = time.perf_counter() - started
        with engine.db.connect() as conn:
            metrics = [
                dict(row)
                for row in conn.execute(
                    "SELECT name,value,data FROM metrics WHERE name LIKE 'model_%'"
                )
            ]
        result = {
            "sources": samples,
            "receipt_ms": receipt,
            "through_processing_seconds": elapsed,
            "source_stages": [
                {k: engine.source(sid)[k] for k in ["mechanical", "model"]}
                for sid in ids
            ],
            "model_tokens": sum(
                row["value"] for row in metrics if row["name"] == "model_tokens"
            ),
            "model_calls": sum(row["name"] == "model_tokens" for row in metrics),
            "scope": "short synthetic explicit-preference sources; extraction and configured conflict roles; successful response usage includes retries; failed requests without usage are not counted",
        }
        result["tokens_per_thousand_sources"] = result["model_tokens"] / samples * 1000
        result["sources_per_second"] = samples / elapsed
        result["currency_cost"] = None
        result["cost_note"] = (
            "Provider prices were not supplied; token usage is measured, currency cost unavailable."
        )
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("samples must be between 1 and 100")
    run(Engine(args.root).settings("models"), args.output, args.samples)
