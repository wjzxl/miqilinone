"""零安装启动器：把 src/ 加入 sys.path，避免 P0 还要先 pip install -e ."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from michelin.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
