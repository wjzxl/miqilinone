"""P2 真实编码 worker：在干净 worktree 里改文件，产出 unified diff（不是分析文本）。

P0 的 ReadonlyWorker 只产文本；P2 换成真实 patch，从而让 verifier 可以用
`git apply` 在另一个干净 worktree 上重放产物、再驱动真实应用验证。
模型只负责给「改法」，真正的写文件与 diff 由确定性的 git 完成 —— 这也是
「给 AI 手和眼」的一部分：产物必须是可机械检查的，不是一段自述。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass

from ..chief.policy import Kickoff
from ..context.sources import SourceDocument
from ..models import ModelAdapter, ModelResponse
from ..workspace.seed import GitWorkspace, Worktree

_RATE_RE = re.compile(r"TAX_RATE\s*=\s*([0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True, slots=True)
class GitWorkerOutput:
    patch: str
    artifact_body: str
    self_check_output: str
    model_response: ModelResponse
    tool_calls: tuple[dict, ...]
    worktree: Worktree
    head_sha: str

    @property
    def diff_hash(self) -> str:
        from ..platform.envelope import sha256_text
        return sha256_text(self.patch)


class GitCodeWorker:
    """每个 attempt 开一个全新 worktree + 全新模型调用，不复用上一轮的失败上下文。"""

    def __init__(self, model: ModelAdapter, workspace: GitWorkspace):
        self.model = model
        self.workspace = workspace

    def run(self, kickoff: Kickoff, sources: list[SourceDocument]) -> GitWorkerOutput:
        tree = self.workspace.worktree(f"author-a{kickoff.attempt}")
        original = tree.read_file("shop/pricing.py")

        source_block = "\n\n".join(
            f"--- source_hash={s.source_hash} ---\n{s.text}" for s in sources
        )
        prompt = (
            f"任务意图：{kickoff.intent}\n"
            f"当前 shop/pricing.py 原文：\n{original}\n\n"
            f"一手上下文（只读，引用时带 source_hash）：\n{source_block}\n\n"
            "请给出修改后的 TAX_RATE 赋值行，格式必须包含 `TAX_RATE = <数值>`。"
        )
        response = self.model.complete(prompt)
        match = _RATE_RE.search(response.text)
        target = match.group(1) if match else None

        if target is None:
            raise ValueError("worker 未给出可解析的 TAX_RATE 赋值，交付物不可机械检查")

        patched = _RATE_RE.sub(f"TAX_RATE = {target}", original, count=1)
        tree.write_file("shop/pricing.py", patched)
        patch = tree.diff("shop/pricing.py")

        # 自陈：worker 自己跑一次 CLI。verifier 会整个丢弃这段，独立重跑。
        env_path = tree.path / ".p2-selfcheck-data"
        probe = subprocess.run(
            [sys.executable, "-S", "-m", "shop.cli", "total", "10", "20"],
            cwd=str(tree.path), capture_output=True, text=True, timeout=30,
            env={**os.environ,
                 "PYTHONPATH": str(tree.path),
                 "SHOP_DATA_DIR": str(env_path)},
        )
        self_check = (
            f"WORKER_SELF_REPORT PASS (self-reported) stdout={probe.stdout.strip()} "
            f"exit={probe.returncode} | patch_bytes={len(patch.encode('utf-8'))}"
        )
        return GitWorkerOutput(
            patch=patch,
            artifact_body=patch,
            self_check_output=self_check,
            model_response=response,
            tool_calls=(
                {"tool": "write_file", "path": "shop/pricing.py", "args_hash": str(len(patched))},
                {"tool": "git_diff", "args_hash": str(len(patch))},
            ),
            worktree=tree,
            head_sha=tree.head_sha(),
        )
