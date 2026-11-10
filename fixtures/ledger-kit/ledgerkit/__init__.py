"""ledger-kit：一个极小的记账核心，纯函数、零第三方依赖。"""

from .core import balance, total

__version__ = "0.3.2"
__all__ = ["total", "balance"]
