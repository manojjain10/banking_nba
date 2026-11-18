#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Cloudera AI Workbench AMP setup step: install Python & optional UI assets.

Portable across repository layouts (``/home/cdsw``, ``/home/cdsw/<subdir>``, or
local checkouts). Pre-compiled frontend bundles under ``app/web/static/js`` are
already committed so Node.js/npm is optional at deploy time.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def resolve_project_root() -> Path:
    """Locate the blueprint directory containing ``requirements.txt``."""
    candidates: List[Path] = []
    if "__file__" in globals():
        here = Path(__file__).resolve().parent
        candidates.extend([here.parent, here])

    cwd = Path.cwd().resolve()
    candidates.extend(
        [
            cwd,
            cwd / "AI_Blueprint_Observability-Next-Best-Action",
            Path("/home/cdsw"),
            Path("/home/cdsw/AI_Blueprint_Observability-Next-Best-Action"),
        ]
    )

    for candidate in candidates:
        if (candidate / "requirements.txt").is_file() and (
            candidate / "app" / "server.py"
        ).is_file():
            return candidate

    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"Could not locate project root containing requirements.txt. Searched: {searched}"
    )


def main() -> None:
    project_root = resolve_project_root()
    req_file = project_root / "requirements.txt"

    print(f"[setup] Installing Python dependencies from {req_file} ...", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--no-cache-dir",
            "-r",
            str(req_file),
        ],
        cwd=str(project_root),
        check=True,
    )

    web_dir = project_root / "app" / "web"
    compiled_entry = web_dir / "static" / "js" / "main.js"
    npm_bin = shutil.which("npm")

    if compiled_entry.is_file():
        print(
            f"[setup] Verified prebuilt UI assets at {compiled_entry}.",
            flush=True,
        )
    elif npm_bin and (web_dir / "package.json").is_file():
        print(f"[setup] Building TypeScript UI in {web_dir} ...", flush=True)
        subprocess.run([npm_bin, "ci"], cwd=str(web_dir), check=True)
        subprocess.run([npm_bin, "run", "build"], cwd=str(web_dir), check=True)
    else:
        print(
            f"[setup] WARNING: {compiled_entry} not found and npm is unavailable; "
            "API routes will still run.",
            flush=True,
        )

    print("[setup] Cloudera AI dependency setup complete.", flush=True)


if __name__ == "__main__":
    main()
