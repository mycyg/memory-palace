"""Run synthetic answer replay with explicitly configured model roles.

Example: eventmem api configure_models ...; python scripts/model_replay.py --root /tmp/model-config --output result.json
The script never reads production memories; only the model settings are copied.
"""

import argparse, json, tempfile, time
from pathlib import Path
from eventmem.core import Engine, SourceInput
from eventmem.core.jobs import Worker
from eventmem.core.evaluation import evaluate

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = Engine(args.root).settings("models")
    with tempfile.TemporaryDirectory() as temp:
        engine = Engine(temp)
        engine.settings("models", config)
        start = time.perf_counter()
        source = engine.receive(
            SourceInput(
                namespace="model-e2e",
                key="1",
                text="I prefer concise technical explanations.",
                extract=True,
            )
        )
        receipt_ms = (time.perf_counter() - start) * 1000
        worker = Worker(engine)
        while worker.run_once():
            pass
        model_ms = (time.perf_counter() - start) * 1000
        result = evaluate(args.output, answer_engine=engine)
        result["model_pipeline"] = {
            "receipt_ms": receipt_ms,
            "through_extraction_ms": model_ms,
            "mechanical": engine.source(source["id"])["mechanical"],
            "model": engine.source(source["id"])["model"],
        }
        result["model_usage"] = engine.overview()["model_tokens"]
        result["model_usage_scope"] = (
            "current process including extraction and retries; resumed cells from earlier processes are excluded"
        )
        result["model_config_fingerprints"] = {
            role: __import__("hashlib")
            .sha256(json.dumps(c, sort_keys=True).encode())
            .hexdigest()
            for role, c in config.items()
        }
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("Saved synthetic model replay; source stages:", result["model_pipeline"])
