"""Reproducible, model-free work-memory retrieval fixture.

The fixed corpus and query set are synthetic. This measures local retrieval and
context assembly on the machine running the script, not answer quality or a
cold-disk guarantee.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from eventmem.core import Engine, RecallRequest, SourceInput
from eventmem.core.models import Scope
from eventmem.core.providers import Providers

RECORDS = 240
QUERIES = 40
BUDGET = 800
LIMIT = 8
OCCURRED_AT = "2026-01-01T00:00:00Z"


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[int((len(ordered) - 1) * 0.95)],
        "raw_ms": values,
    }


def run() -> dict:
    model_calls = []

    def forbid_model(*_args, **_kwargs):
        model_calls.append(1)
        raise AssertionError("The benchmark must not call a model")

    original_request = Providers.request
    Providers.request = forbid_model
    try:
        with tempfile.TemporaryDirectory(prefix="memorypalace-work-benchmark-") as root:
            engine = Engine(root)
            scope = Scope(project="benchmark", persona="worker")
            expected = {}
            for index in range(RECORDS):
                source = engine.receive(
                    SourceInput(
                        namespace="benchmark",
                        key=str(index),
                        scope=scope,
                        text=(
                            f"Task project{index:04d}: build artifact{index:04d}. "
                            "Decision: validate the saved output before delivery. "
                            "Result: completed."
                        ),
                        occurred_at=OCCURRED_AT,
                        extract=False,
                    )
                )
                expected[index] = engine.source(source["id"])["record_ids"][0]

            cache_cleared_ms = []
            warm_ms = []
            token_counts = []
            top8_hits = 0
            results = []
            for index in range(QUERIES):
                query = RecallRequest(
                    scope=scope, query=f"project{index:04d}", budget=BUDGET, limit=LIMIT
                )
                engine.cache.clear()
                started = time.perf_counter()
                first = engine.recall(query)
                cache_cleared_ms.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                warm = engine.recall(query)
                warm_ms.append((time.perf_counter() - started) * 1000)
                ids = [item["id"] for item in warm["items"]]
                top8_hits += expected[index] in ids
                token_counts.append(warm["tokens"])
                results.append({"query": query.query, "hit": expected[index] in ids, "ids": ids})
                if first["tokens"] > BUDGET or warm["tokens"] > BUDGET:
                    raise AssertionError("Recall exceeded the declared token budget")
    finally:
        Providers.request = original_request

    return {
        "benchmark": "synthetic work-memory retrieval",
        "method": (
            "240 fixed synthetic sources; 40 distinct exact-cue queries; "
            "cache_cleared clears the process cache, not the OS filesystem cache; "
            "warm repeats each query in the same process"
        ),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
        },
        "corpus_records": RECORDS,
        "queries": QUERIES,
        "budget_tokens": BUDGET,
        "limit": LIMIT,
        "top8_hits": top8_hits,
        "cold_ms": distribution(cache_cleared_ms),
        "warm_ms": distribution(warm_ms),
        "context_tokens": {
            "mean": statistics.mean(token_counts),
            "max": max(token_counts),
            "raw": token_counts,
        },
        "model_calls": len(model_calls),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "results"}
    for section in ("cold_ms", "warm_ms"):
        summary[section] = {
            key: value for key, value in summary[section].items() if key != "raw_ms"
        }
    summary["context_tokens"] = {
        key: value for key, value in summary["context_tokens"].items() if key != "raw"
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
