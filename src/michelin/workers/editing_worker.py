"""多文件编辑 worker：让模型自己决定改哪些文件，但改不了不该改的。

P2 的 `GitCodeWorker` 是「单文件 + 一次性」：总厨点名 `shop/pricing.py`，
模型给出一行赋值，正则替换，git 生成 diff。它验证的是「模型能不能按格式
交出一个可机械检查的改动」。

本模块把这件事推进一步，也是更接近真实工程的一层：

* **工具循环**：模型可以先 `READ` 文件再决定改什么 —— 它自己决定改哪些文件，
  不是被总厨点名；
* **范围强制**：总厨划定的可写边界（glob）是**硬约束**，越界即拒，
  不是「提醒一下」；
* **产出仍是机器可查的**：模型给的是「文件 → 新内容」，写文件与 diff 由
  确定性 git 完成（与 P2 同一条纪律：给 AI 手和眼）。

协议（纯文本，模型容易照着写）::

    READ shop/cli.py

    === BEGIN FILE: shop/pricing.py ===
    ...完整的新文件内容...
    === END FILE ===

解析必须**严格**：块不成对、路径越界、内容不完整，一律判为「产出不可机械检查」
（`WorkerOutputError`），由总厨换一个全新 worker 重来，而不是猜作者想干嘛。
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..chief.policy import Kickoff
from ..context.sources import SourceDocument
from ..models import ModelAdapter, ModelResponse
from ..workspace.seed import GitWorkspace, Worktree
from .git_worker import WorkerOutputError

BEGIN_RE = re.compile(r"^===\s*BEGIN FILE:\s*(?P<path>[^\s=]+)\s*===\s*$")
END_MARK = "=== END FILE ==="
READ_RE = re.compile(r"^READ\s+(?P<path>[^\s]+)\s*$", re.MULTILINE)

SYSTEM_PROMPT = """你是一个在真实代码库里工作的工程师 agent。

你有两种动作，只能用这两种形式表达：

1) 读文件（了解现状后再决定改什么）：
READ <仓库内相对路径>

2) 提交编辑（给出该文件的**完整新内容**，不是 diff、不是片段）：
=== BEGIN FILE: <仓库内相对路径> ===
<完整文件内容>
=== END FILE ===

规则：
- 只能修改授权范围内的文件；范围外的一律不许碰。
- 一次回复可以包含多个 READ，也可以包含多个 EDIT 块。
- 每个文件的内容要完整、能独立通过语法检查。
- 不要输出解释性散文，不要用 markdown 代码块包裹协议。
"""


@dataclass(frozen=True, slots=True)
class EditBlock:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class WorkerAction:
    """模型一轮的产出：若干读请求 + 若干编辑块。"""

    reads: tuple[str, ...] = ()
    edits: tuple[EditBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class MultiFileWorkerOutput:
    patch: str
    artifact_body: str
    self_check_output: str
    model_response: ModelResponse
    tool_calls: tuple[dict, ...]
    worktree: Worktree
    head_sha: str
    reads: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    rejected: tuple[dict, ...] = ()
    rounds: int = 0
    total_usage: dict = field(default_factory=dict)

    @property
    def diff_hash(self) -> str:
        """与 GitWorkerOutput 同名同义：产物哈希 = diff 的哈希。

        编排层只认这一个接口，两种 worker 可以互换。
        """
        from ..platform.envelope import sha256_text
        return sha256_text(self.patch)


def parse_actions(text: str) -> WorkerAction:
    """把模型的一轮回复解析成动作。解析不了就抛 WorkerOutputError（不猜）。"""
    reads = tuple(m.group("path") for m in READ_RE.finditer(text))
    edits: list[EditBlock] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        begin = BEGIN_RE.match(lines[i].strip())
        if not begin:
            i += 1
            continue
        path = begin.group("path")
        body: list[str] = []
        i += 1
        closed = False
        while i < len(lines):
            if lines[i].strip() == END_MARK:
                closed = True
                i += 1
                break
            body.append(lines[i])
            i += 1
        if not closed:
            raise WorkerOutputError(
                f"编辑块没有闭合（{path}）：缺少 '{END_MARK}'，内容不可机械检查"
            )
        edits.append(EditBlock(path=path, content="\n".join(body) + "\n"))
    if not reads and not edits:
        raise WorkerOutputError(
            "模型这一轮既没有 READ 也没有 EDIT 块，无法机械解析"
        )
    return WorkerAction(reads=reads, edits=tuple(edits))


def _safe_relpath(path: str) -> str:
    """把模型给的路径规整成仓库内相对路径；越界直接拒绝。"""
    raw = path.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise WorkerOutputError(f"路径 {path!r} 是绝对路径，不允许")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WorkerOutputError(f"路径 {path!r} 试图越出仓库，不允许")
    if not parts:
        raise WorkerOutputError(f"路径 {path!r} 为空")
    return "/".join(parts)


def _in_scope(path: str, patterns: tuple[str, ...]) -> bool:
    """可写范围判定：glob 匹配。范围为空 = 不许改任何文件。"""
    if not patterns:
        return False
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # `shop/**` 也应匹配 `shop/a/b.py`，fnmatch 的 * 会跨 /，已覆盖；
        # `shop/*` 不应匹配 `shop/a/b.py` —— 用 fnmatch 会误判为匹配。
        if pattern.endswith("/**") and path.startswith(pattern[:-2]):
            return True
    return False


class MultiFileCodeWorker:
    """有界工具循环的多文件编辑 worker。

    每一轮都是一次真实模型调用（计费、计预算），轮数上限由 `max_rounds` 控制；
    没有轮数上限的工具循环就是「烧钱不设防」。
    """

    def __init__(
        self,
        model: ModelAdapter,
        workspace: GitWorkspace,
        *,
        max_rounds: int = 6,
        max_read_bytes: int = 20000,
    ):
        self.model = model
        self.workspace = workspace
        self.max_rounds = max_rounds
        self.max_read_bytes = max_read_bytes

    # ---------- 工具 ----------
    def _read(self, tree: Worktree, raw_path: str) -> str:
        rel = _safe_relpath(raw_path)
        target = (tree.path / rel).resolve()
        if not str(target).startswith(str(tree.path.resolve())):
            raise WorkerOutputError(f"读取路径 {raw_path!r} 越出 worktree")
        if not target.is_file():
            raise WorkerOutputError(f"文件不存在：{rel}")
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > self.max_read_bytes:
            content = content[:self.max_read_bytes] + "\n... (截断)\n"
        return f"--- FILE: {rel} ---\n{content}\n--- END FILE: {rel} ---"

    def _list_files(self, tree: Worktree) -> str:
        files = sorted(
            p.relative_to(tree.path).as_posix()
            for p in tree.path.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        return "--- REPO FILES ---\n" + "\n".join(files)

    # ---------- 主流程 ----------
    def run(self, kickoff: Kickoff, sources: list[SourceDocument]) -> MultiFileWorkerOutput:
        tree = self.workspace.worktree(f"author-m{kickoff.attempt}")
        scope = tuple(kickoff.writable_paths)
        source_block = "\n\n".join(
            f"--- source_hash={s.source_hash} ---\n{s.text}" for s in sources
        )
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"任务意图：{kickoff.intent}\n\n"
                f"可写范围（只能改这些）：{', '.join(scope) or '（无）'}\n\n"
                f"一手上下文（只读，引用时带 source_hash）：\n{source_block}\n\n"
                f"仓库当前文件列表：\n{self._list_files(tree)}\n\n"
                "先 READ 你需要看的文件，再决定改哪些文件并提交 EDIT。"
                "不确定就看，不要凭空猜文件内容。"
            )},
        ]

        reads: list[str] = []
        rejected: list[dict] = []
        last_response: ModelResponse | None = None
        usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                       "total_tokens": 0, "external_requests": 0}

        for round_index in range(1, self.max_rounds + 1):
            response = self.model.complete_chat(messages)
            last_response = response
            for key in usage_total:
                usage_total[key] += getattr(response.usage, key, 0)
            try:
                action = parse_actions(response.text)
            except WorkerOutputError as exc:
                # 解析失败不猜：把协议要求再喂回去，让模型重来。
                messages.append({"role": "assistant", "content": response.text})
                messages.append({"role": "user", "content": (
                    f"你的上一条回复无法解析：{exc}\n"
                    "请严格按协议重发：READ <路径> 或 "
                    "=== BEGIN FILE: <路径> === ... === END FILE ==="
                )})
                rejected.append({"round": round_index, "reason": str(exc)})
                continue

            if action.edits:
                edited = self._apply(tree, action.edits, scope, round_index, rejected)
                if not edited:
                    messages.append({"role": "assistant", "content": response.text})
                    messages.append({"role": "user", "content": (
                        "所有 EDIT 都超出授权范围或不可用。请只提交范围内的文件。"
                    )})
                    continue
                patch = tree.diff_all()
                probe = self._self_check(tree)
                return MultiFileWorkerOutput(
                    patch=patch,
                    artifact_body=patch,
                    self_check_output=probe,
                    model_response=response,
                    tool_calls=(
                        {"tool": "read_file", "paths": list(reads)},
                        {"tool": "write_file", "paths": list(edited)},
                        {"tool": "git_diff", "args_hash": str(len(patch))},
                    ),
                    worktree=tree,
                    head_sha=tree.head_sha(),
                    reads=tuple(reads),
                    edited=tuple(edited),
                    rejected=tuple(rejected),
                    rounds=round_index,
                    total_usage=usage_total,
                )

            results: list[str] = []
            for raw_path in action.reads:
                try:
                    results.append(self._read(tree, raw_path))
                    reads.append(_safe_relpath(raw_path))
                except WorkerOutputError as exc:
                    rejected.append({"round": round_index, "path": raw_path,
                                     "reason": str(exc)})
                    results.append(f"读取失败：{exc}")
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content": "\n\n".join(results)})

        raise WorkerOutputError(
            f"模型在 {self.max_rounds} 轮内没有提交任何可应用的编辑"
            + (f"；被拒动作 {len(rejected)} 次" if rejected else "")
        )

    def _apply(self, tree: Worktree, edits: tuple[EditBlock, ...],
               scope: tuple[str, ...], round_index: int,
               rejected: list[dict]) -> list[str]:
        applied: list[str] = []
        for edit in edits:
            try:
                rel = _safe_relpath(edit.path)
            except WorkerOutputError as exc:
                rejected.append({"round": round_index, "path": edit.path,
                                 "reason": str(exc)})
                continue
            if not _in_scope(rel, scope):
                rejected.append({"round": round_index, "path": rel,
                                 "reason": f"超出授权范围 {list(scope)}"})
                continue
            try:
                compile(edit.content, rel, "exec")
            except SyntaxError as exc:
                rejected.append({"round": round_index, "path": rel,
                                 "reason": f"内容语法错误：{exc}"})
                continue
            tree.write_file(rel, edit.content)
            applied.append(rel)
        return applied

    def _self_check(self, tree: Worktree) -> str:
        """worker 自陈：跑一次真实 CLI。自证不构成裁决，verifier 会整个丢弃。"""
        proc = subprocess.run(
            [sys.executable, "-S", "-m", "shop.cli", "total", "10", "20"],
            cwd=str(tree.path), capture_output=True, text=True, timeout=30,
            env={**os.environ,
                 "PYTHONPATH": str(tree.path),
                 "SHOP_DATA_DIR": str(tree.path / ".selfcheck-data")},
        )
        return (
            f"WORKER_SELF_REPORT PASS (self-reported) stdout={proc.stdout.strip()} "
            f"exit={proc.returncode}"
        )
