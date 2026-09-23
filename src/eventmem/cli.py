"""MemoryPalace command line: one SQLite service. Legacy stores use migrate."""
from .core.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
