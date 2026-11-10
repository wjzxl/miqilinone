"""测试公共路径引导：无需 pip install -e . 即可运行 unittest。"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
