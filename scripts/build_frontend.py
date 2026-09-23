"""Build the console into source and wheel archives."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return

        root = Path(self.root)
        web = root / "src" / "eventmem" / "web"
        if (root / ".git").exists():
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError("Building a wheel from a Git checkout requires Node.js/npm")
            console = root / "console"
            if not (console / "node_modules").is_dir():
                subprocess.run([npm, "ci"], cwd=console, check=True)
            subprocess.run([npm, "run", "build"], cwd=console, check=True)

        if not (web / "index.html").is_file() or not list((web / "assets").glob("*.js")):
            raise RuntimeError("The release archive is missing built console assets")
        build_data.setdefault("artifacts", []).append("/src/eventmem/web/**")
