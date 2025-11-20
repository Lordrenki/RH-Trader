"""Convenience entry point for running the trader bot.

Ensures the local ``src`` directory is importable so that running
``python bot.py`` works without installing the package.
"""
from __future__ import annotations

from pathlib import Path
import sys


def _ensure_src_on_path() -> None:
    project_root = Path(__file__).resolve().parent
    src_dir = project_root / "src"
    if src_dir.exists():
        sys.path.insert(0, str(src_dir))


_ensure_src_on_path()
from rh_trader.bot import run_bot


if __name__ == "__main__":
    run_bot()
