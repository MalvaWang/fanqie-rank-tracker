#!/usr/bin/env python3
"""Wrapper for the project-level Fanqie rank tracker."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    skill_dir = Path(__file__).resolve().parents[1]
    default_root = skill_dir.parents[1]
    root = Path(os.environ.get("FANQIE_RANK_TRACKER_ROOT", default_root)).expanduser().resolve()
    script = root / "fanqie_rank_agent.py"
    if not script.exists():
        sys.stderr.write(f"fanqie_rank_agent.py not found under {root}\n")
        return 2
    return subprocess.call([sys.executable, str(script), *argv], cwd=str(root))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
