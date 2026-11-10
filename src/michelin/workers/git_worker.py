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

# 注意：这个正则**不能**在数值后面加 `\s*` —— 它会贪婪吃掉换行，
# 把 `TAX_RATE = 0.10\n\n\ndef ...` 替换成 `TAX_RATE = 0.06def ...`，
# 产出一个语法错误的文件。真实模型跑通 P2 时才暴露过这个坑。
# 百分号单独检测（见 _has_percent_suffix），不进正则。
_RATE_RE = re.compile(r"TAX_RATE\s*=\s*([0-9]+(?:\.[0-9]+)?)")


def _has_percent_suffix(text: str, match: re.Match) -> bool:
    """数值后面是否紧跟百分号（`TAX_RATE = 6%`）。"""
    tail = text[match.end():match.end() + 4].lstrip()
    return tail.startswith("%")


class WorkerOutputError(RuntimeError):
    """worker 的产出无法被机械检查。

    这**不是**流水线崩溃，而是一次可返工的失败：总厨会换一个全新 worker
    重来（而不是拿一个不可检查的产物去裁决）。真实模型尤其会犯这种错 ——
    例如把 6% 写成 `TAX_RATE = 6`。
    """


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
        # 给上下文与可检查的输出契约，不写「一步步怎么做」的死指令。
        prompt = (
            f"任务意图：{kickoff.intent}\n"
            f"当前 shop/pricing.py 原文：\n{original}\n\n"
            f"一手上下文（只读，引用时带 source_hash）：\n{source_block}\n\n"
            "输出契约（必须可被机械检查）：\n"
            "1. 只回答一行赋值，格式为 `TAX_RATE = <数值>`；\n"
            "2. 数值是**小数比例**（6% 写成 0.06），不要带百分号、不要带千分位；\n"
            "3. 不要输出整段文件、不要用 markdown 代码块包裹赋值行；\n"
            "4. 回答里附一句依据，引用你用的 source_hash。\n"
            "验收由独立验证者在干净 worktree 上重放你的 diff 并驱动真实 CLI 判定，"
            "你的自述不作数。"
        )
        response = self.model.complete(prompt)
        match = _RATE_RE.search(response.text)
        target = match.group(1) if match else None

        if target is None:
            raise WorkerOutputError(
                "worker 未给出可解析的 TAX_RATE 赋值，交付物不可机械检查"
            )
        if _has_percent_suffix(response.text, match):
            raise WorkerOutputError(
                f"worker 给出了百分数（TAX_RATE = {target}%），必须是 0.06 这样的小数比例"
            )
        if float(target) > 1:
            raise WorkerOutputError(
                f"worker 给出的税率 {target} 不是小数比例（6% 应写成 0.06）"
            )

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
