"""P5 运行时层：每任务隔离执行、可观测 trace、成本预算与熔断。

与 P0 事件脊的分工（见 store.py 开头）：事件脊是不可变、长期保留的**执行日志**；
这里的 span 是可采样、可脱敏、可导出 OTel 的**观测数据**，两者不能互相替代。
本包不改 P0–P4 的任何协议，所有状态落在 run 自己的目录下。
"""

from .sandbox import IsolationReceipt, SandboxPolicy, container_command, isolated_run
from .telemetry import SpanLog, Tracer
from .budget import Budget, BudgetExceeded, BudgetGuard, Charge
from .harness import RunHarness

__all__ = [
    "IsolationReceipt", "SandboxPolicy", "container_command", "isolated_run",
    "SpanLog", "Tracer", "Budget", "BudgetExceeded", "BudgetGuard", "Charge",
    "RunHarness",
]
