"""Check that the 2.0 wheel is usable without a checkout or private files."""

from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    wheels = sorted(Path("dist").glob("eventmem-2.0.0-*.whl"))
    wheel = Path(sys.argv[1]) if len(sys.argv) > 1 else (wheels[-1] if wheels else None)
    if wheel is None or not wheel.is_file():
        raise SystemExit("No eventmem 2.0 wheel found in dist/")

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            "eventmem/__init__.py",
            "eventmem/cli.py",
            "eventmem/core/engine.py",
            "eventmem/core/api.py",
            "eventmem/core/db.py",
            "eventmem/sdk/__init__.py",
            "eventmem/sdk/__init__.pyi",
            "eventmem/sdk/_operations.py",
            "eventmem/sdk/py.typed",
            "eventmem/web/index.html",
        }
        missing = sorted(required - names)
        if missing:
            raise AssertionError(f"Missing package files: {missing}")
        if not any(name.startswith("eventmem/web/assets/") and name.endswith(".js") for name in names):
            raise AssertionError("Missing built console JavaScript")
        if not any(name.startswith("eventmem/web/assets/") and name.endswith(".css") for name in names):
            raise AssertionError("Missing built console CSS")

        prefix = next((name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/METADATA")), None)
        if prefix != "eventmem-2.0.0.dist-info":
            raise AssertionError(f"Unexpected distribution metadata: {prefix}")
        metadata = archive.read(f"{prefix}/METADATA").decode()
        entry_points = archive.read(f"{prefix}/entry_points.txt").decode()
        if "Name: eventmem" not in metadata or "Version: 2.0.0" not in metadata:
            raise AssertionError("Wheel name/version metadata is wrong")
        if "eventmem = eventmem.cli:main" not in entry_points:
            raise AssertionError("Missing eventmem command")
        base_requirements = [
            line.lower()
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist:") and "extra ==" not in line
        ]
        for optional in ("sentence-transformers", "lancedb", "igraph", "docling"):
            if any(optional in line for line in base_requirements):
                raise AssertionError(f"Optional model or index package is required: {optional}")
        unexpected_roots = sorted(
            name
            for name in names
            if not name.startswith(("eventmem/", "eventmem-2.0.0.dist-info/"))
        )
        if unexpected_roots:
            raise AssertionError(f"Unexpected package roots: {unexpected_roots}")
        forbidden = (
            "eventmem/core/self_knowledge.py",
            ".env",
            "memory.sqlite3",
            "__pycache__/",
            ".pyc",
        )
        leaked = sorted(name for name in names if any(token in name for token in forbidden))
        if leaked:
            raise AssertionError(f"Private or retired files in wheel: {leaked}")

    print(f"Checked {wheel}: core, CLI, typed SDK, console, and clean package contents")


if __name__ == "__main__":
    main()
