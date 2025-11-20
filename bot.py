"""Convenience entry point for running the trader bot.

This module ensures the local ``src`` directory is on ``PYTHONPATH`` so the
package can be imported without an editable/install step (useful for quick
local runs).
"""

from pathlib import Path
import sys

# Allow running directly from the repo root without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from rh_trader.bot import run_bot

if __name__ == "__main__":
    run_bot()
