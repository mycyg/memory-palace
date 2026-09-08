"""Additional query workloads against a previously generated isolated scale fixture."""

from __future__ import annotations
import argparse, json, subprocess, sys, time, threading, socket
from pathlib import Path
from eventmem.core import Engine, RecallRequest
from eventmem.core.evaluation import distribution
from eventmem.core.retrieval import tokens


def probe(root):
    start = time.perf_counter()
    engine = Engine(root)
    initialized = time.perf_counter()
    first = engine.recall(RecallRequest(query="allocation"))
    return {
        "initialization_ms": (initialized - start) * 1000,
        "first_recall_ms": (time.perf_counter() - initialized) * 1000,
        "total_ms": (time.perf_counter() - start) * 1000,
        "tokens": first["tokens"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--probe", action="store_true")
    a = p.parse_args()
    if a.probe:
        print(json.dumps(probe(a.root)))
        raise SystemExit
    cold = [
        json.loads(
            subprocess.check_output(
                [sys.executable, __file__, "--root", str(a.root), "--probe"], text=True
            )
        )
        for _ in range(5)
    ]
    engine = Engine(a.root)
    report = {
        "fresh_process": cold,
        "cold_cache_note": "New Python processes; OS filesystem cache is not flushed.",
        "workloads": {},
    }
    for name, queries in {
        "identifiers": [f"item_{i}" for i in range(100)],
        "topics": [f"topic_{i}" for i in range(100)],
        "common_terms": [
            "allocation",
            "build deployment",
            "configuration reference",
            "port validation",
            "deployment result",
        ]
        * 20,
        "startup": [""] * 100,
    }.items():
        elapsed = []
        for q in queries:
            start = time.perf_counter()
            r = engine.recall(RecallRequest(query=q, budget=2000))
            elapsed.append((time.perf_counter() - start) * 1000)
            assert tokens(r["text"]) <= 2000
        report["workloads"][name] = distribution(elapsed)
    import uvicorn, httpx
    from eventmem.core.api import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(engine=engine, token="synthetic-benchmark-only", workers=False),
            log_level="error",
        )
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    elapsed = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer synthetic-benchmark-only"},
        ) as client:
            for i in range(100):
                start = time.perf_counter()
                r = client.post(
                    "/v1/recall", json={"query": "allocation", "budget": 2000}
                )
                r.raise_for_status()
                elapsed.append((time.perf_counter() - start) * 1000)
        report["http_loopback"] = distribution(elapsed)
    finally:
        server.should_exit = True
        thread.join(5)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
