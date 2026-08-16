"""Shared in-process run flag for the dashboard and JSON API."""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import Optional

from owly.config import PROJECT_ROOT

_run_lock = threading.Lock()
_run_in_progress = False


def is_run_in_progress() -> bool:
    with _run_lock:
        return _run_in_progress


def reset_for_tests() -> None:
    """Clear the busy flag between unit tests."""
    global _run_in_progress
    with _run_lock:
        _run_in_progress = False


def start_run(edition_slot: Optional[str] = None) -> str:
    """Start a background edition run. Returns 'started' or 'busy'."""
    global _run_in_progress
    with _run_lock:
        if _run_in_progress:
            return "busy"
        _run_in_progress = True
    thread = threading.Thread(
        target=_run_edition_background,
        kwargs={"edition_slot": edition_slot},
        daemon=True,
    )
    thread.start()
    return "started"


def _run_edition_background(edition_slot: Optional[str] = None) -> None:
    global _run_in_progress
    try:
        cmd = [sys.executable, "-m", "owly.run"]
        if edition_slot:
            cmd.extend(["--edition", edition_slot])
        subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
    finally:
        with _run_lock:
            _run_in_progress = False
