"""L0 接入层：webhook 验签、幂等去重、DLQ 对账与异步投递。

webhook 是 at-least-once 的「提示」；真正的任务状态在 append-only events 里。
"""
