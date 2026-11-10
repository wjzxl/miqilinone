"""L3 执行层：P0 只读 worker。

每个任务一个干净环境、一个全新上下文；返工时开的是**新 worker**，
不复用上一轮的会话（pstack: 完成一个补一个，别让失败的上下文污染重试）。

本文件刻意保留 `self_check_output`：worker 可以自证，但架构文档 §L3 明确
「自证不构成 verdict」。verifier 会把这个字段当成不可信输入并独立重跑门禁。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..chief.policy import EvidenceSpec, Kickoff
from ..context.sources import SourceDocument
from ..models import ModelAdapter, ModelResponse


@dataclass(frozen=True, slots=True)
class WorkerOutput:
    artifact_body: str
    self_check_output: str
    model_response: ModelResponse
    tool_calls: tuple[dict, ...]


class Worker(Protocol):
    def run(self, kickoff: Kickoff, sources: list[SourceDocument]) -> WorkerOutput: ...


class ReadonlyWorker:
    """只读任务：不写任何文件、不调用任何写工具。

    P0 的「diff」实际是 worker 产出的分析文本（无 git 仓库），
    P2 接上 git worktree 后换成真实 `git diff`，上层协议不变。
    """

    def __init__(self, model: ModelAdapter, *, evidence_marker: str = "EVIDENCE:ok"):
        self.model = model
        self.evidence_marker = evidence_marker

    def run(self, kickoff: Kickoff, sources: list[SourceDocument]) -> WorkerOutput:
        if kickoff.writable_paths:
            raise PermissionError("P0 只读任务不得声明可写路径")

        # 给一手原文 + 可验证证据规格，不给二手摘要、不下「800 字、第一句怎么写」的死指令。
        source_block = "\n\n".join(
            f"--- source_hash={s.source_hash} uri={s.source_uri} ---\n{s.text}"
            for s in sources
        )
        prompt = (
            f"任务意图：{kickoff.intent}\n"
            f"证据规格：产出必须包含 {self.evidence_marker}，并说明依据来自哪条 source_hash。\n"
            f"禁止动作：{', '.join(kickoff.forbidden_actions)}\n\n"
            f"一手原文（只读，引用时带 source_hash）：\n{source_block}\n"
        )
        response = self.model.complete(prompt)
        artifact_body = (
            f"# 分析产出 (attempt={kickoff.attempt})\n\n"
            f"{response.text}\n\n"
            f"来源引用：{', '.join(s.source_hash[:12] for s in sources)}\n"
        )
        # worker 自己跑一遍「与 diff/治理文档有关」的规格并自报结果 —— verifier 会忽略它并独立重跑。
        # 独立重跑类谓词（test_output_contains）worker 无权自评，跳过。
        checks = [
            p.check(_MiniBundle(artifact_body, self.evidence_marker))
            for p in kickoff.evidence_spec.predicates
            if not p.kind.startswith("test_output")
        ]
        self_check = f"WORKER_SELF_REPORT {'PASS' if all(ok for ok, _ in checks) else 'FAIL'} " \
                     f"({sum(ok for ok, _ in checks)}/{len(checks)} checks, self-reported)"
        return WorkerOutput(
            artifact_body=artifact_body,
            self_check_output=self_check,
            model_response=response,
            tool_calls=({"tool": "read_source", "args_hash": "read-only", "count": len(sources)},),
        )


@dataclass(frozen=True, slots=True)
class _MiniBundle:
    """仅供 worker 自检的鸭子类型，避免 worker 接触真正的 HoldoutBundle。"""

    diff: str
    test_output: str = "WORKER_SELF_REPORT PASS"
    governance_docs: tuple[str, ...] = ("只读任务",)
    issue_body: str = ""
