#****************************************************************************
# (C) Cloudera, Inc. 2019-2026
#  All rights reserved.
#
#  Applicable Open Source License: GNU Affero General Public License v3.0
#***************************************************************************/

"""Portable Cloudera AI Workbench & local application launcher.

Works across standard Cloudera AI Workbench project layouts:
  - Repo cloned directly into ``/home/cdsw`` (``/home/cdsw/app``)
  - Monorepo cloned into ``/home/cdsw`` (``/home/cdsw/AI_Blueprint_Observability-Next-Best-Action/app``)
  - Executed inside a Cloudera AI Application container (IPython or standard Python)
  - Local development from any working directory (``python launch_app.py``)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List


def resolve_app_dir() -> Path:
    """Locate the ``app/`` directory containing ``server.py`` across layouts."""
    candidates: List[Path] = []

    env_dir = os.environ.get("NBA_APP_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser().resolve())

    if "__file__" in globals():
        here = Path(__file__).resolve().parent
        candidates.extend(
            [
                here / "app",
                here / "AI_Blueprint_Observability-Next-Best-Action" / "app",
            ]
        )

    cwd = Path.cwd().resolve()
    candidates.extend(
        [
            cwd / "app",
            cwd / "AI_Blueprint_Observability-Next-Best-Action" / "app",
            Path("/home/cdsw/app"),
            Path("/home/cdsw/AI_Blueprint_Observability-Next-Best-Action/app"),
        ]
    )

    for candidate in candidates:
        if (candidate / "server.py").is_file():
            return candidate

    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"Could not locate app/server.py. Searched: {searched}"
    )


def _prepare_runtime_env() -> dict[str, str]:
    """Build child process environment with safe Impala fallback if unconfigured."""
    env = os.environ.copy()
    backend = env.get("NBA_DB_BACKEND", "sqlite").strip().lower()
    impala_host = env.get("IMPALA_HOST", "").strip()
    cml_conn = env.get("CML_IMPALA_CONNECTION_NAME", "").strip()
    strict_impala = env.get("NBA_STRICT_IMPALA", "0").strip() == "1"

    if backend == "impala" and not impala_host and not cml_conn and not strict_impala:
        print(
            "[launch_app] WARNING: NBA_DB_BACKEND='impala' is set, but neither "
            "IMPALA_HOST nor CML_IMPALA_CONNECTION_NAME is configured. "
            "Falling back to NBA_DB_BACKEND='sqlite' until CDW credentials are set.",
            flush=True,
        )
        env["NBA_DB_BACKEND"] = "sqlite"

    return env


def main() -> None:
    app_dir = resolve_app_dir()
    env = _prepare_runtime_env()

    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{app_dir}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(app_dir)
    )

    skip_bootstrap = env.get("NBA_SKIP_BOOTSTRAP", "0").strip() == "1"
    datagen_script = app_dir / "pii_datagen.py"
    if not skip_bootstrap and datagen_script.is_file():
        print(
            f"[launch_app] Verifying backing store via {datagen_script} --ensure "
            f"(backend={env.get('NBA_DB_BACKEND', 'sqlite')}) ...",
            flush=True,
        )
        subprocess.run(
            [sys.executable, str(datagen_script), "--ensure"],
            cwd=str(app_dir),
            env=env,
            check=True,
        )

    host = env.get("NBA_HOST", "127.0.0.1")
    port = (
        env.get("CDSW_READONLY_PORT")
        or env.get("CDSW_APP_PORT")
        or env.get("NBA_PORT")
        or "8000"
    )

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "server:app",
        "--app-dir",
        str(app_dir),
        "--host",
        host,
        "--port",
        str(port),
    ]
    print(
        f"[launch_app] Starting Governed NBA Workbench on http://{host}:{port} "
        f"(app_dir={app_dir})",
        flush=True,
    )
    try:
        subprocess.run(cmd, cwd=str(app_dir), env=env, check=True)
    except KeyboardInterrupt:
        print("\n[launch_app] Shutting down Governed NBA Workbench.", flush=True)


if __name__ == "__main__":
    main()
