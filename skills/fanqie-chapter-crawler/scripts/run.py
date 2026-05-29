#!/usr/bin/env python3
"""Launch the project Fanqie OCR crawler from this skill."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_project_root() -> Path:
    env_root = os.environ.get("FANQIE_CRAWLER_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if (root / "fanqie_ocr_crawler.py").exists():
            return root

    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "fanqie_ocr_crawler.py").exists():
            return parent
    fallback = Path.cwd().resolve()
    if (fallback / "fanqie_ocr_crawler.py").exists():
        return fallback
    raise SystemExit("Cannot find fanqie_ocr_crawler.py. Set FANQIE_CRAWLER_ROOT to the project root.")


def main(argv: list[str]) -> int:
    root = find_project_root()
    cmd = [sys.executable, str(root / "fanqie_ocr_crawler.py"), *argv]
    return subprocess.call(cmd, cwd=str(root))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
