from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

DEFAULT_BACKGROUND_RUNNER_MODULE = "jelica_core.runtime.background_runner"


def _background_creation_flags() -> int:
    if os.name != "nt":
        return 0
    create_new_process_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0))
    return create_new_process_group | detached_process


def launch_background_runtime(
    *,
    jelica_home: Path | None = None,
    runner_module: str = DEFAULT_BACKGROUND_RUNNER_MODULE,
) -> int:
    """Start one detached Service process and return its process identifier."""

    start_new_session = os.name != "nt"
    normalized_runner_module = runner_module.strip()
    if normalized_runner_module == "":
        raise ValueError("runner_module must not be empty")
    environment = os.environ.copy()
    if jelica_home is not None:
        environment["JELICA_HOME"] = str(jelica_home)

    process = subprocess.Popen(
        [sys.executable, "-m", normalized_runner_module],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=start_new_session,
        creationflags=_background_creation_flags(),
        env=environment,
    )
    return process.pid
