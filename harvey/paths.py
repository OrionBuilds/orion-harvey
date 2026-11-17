"""Canonical filesystem locations for the Harvey checkout.

Harvey is repo-centric: config (harvey.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Harvey would silently read/write the wrong files.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Emergency stop: while this file exists nothing is sent on any channel.
# Replies are still read, so opt-outs and stop-on-reply keep working.
STOP_FILE = PROJECT_ROOT / "STOP"


def stop_requested() -> bool:
    return Path(os.environ.get("HARVEY_STOP_FILE") or STOP_FILE).exists()
