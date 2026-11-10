#!/usr/bin/env python3
"""真实 git PR 的增量文件解析 —— 门禁不再靠「临时目录里注入一段代码」模拟 PR。

为什么单独一个模块：P4 的 `_pr_files()` 只会 `git diff --name-only origin/base...HEAD`，
那是 GitHub Actions 的一次性写法，本地无法复现，也没有处理重命名 / 删除 / NUL 文件名。
这里提供同一个可测试的纯函数，本地 fixture 仓库与 CI 的 pull_request 事件共用。

增量语义（很重要）：只拦 **本次 PR 新引入或改动** 的违规。历史遗留的违规文件
如果没被这个 PR 碰过，本 PR 不因它变红；否则任何老仓库都无法渐进收敛。
对照证据由 `ci/gate.py` 的「全量扫描会红 / 增量扫描只红新增」给出。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChangedFile:
    status: str          # A / M / R100 / C80 / D / T / U
    path: str           # 新路径（R/C 取目标路径）
    old_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PRDiff:
    repo: str
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    merge_base: str
    changed: tuple[ChangedFile, ...]
    python_files: tuple[str, ...]
    source: str
    tree_files: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "base_ref": self.base_ref, "head_ref": self.head_ref,
            "base_sha": self.base_sha, "head_sha": self.head_sha,
            "merge_base": self.merge_base, "source": self.source,
            "changed": [c.to_dict() for c in self.changed],
            "python_files": list(self.python_files),
            "tree_files_at_head": len(self.tree_files),
        }

    def materialize(self, out_dir: Path) -> Path:
        """把 head commit 里变更过的 .py 原样导出，供 ast-grep 这类磁盘扫描器消费。

        不能拿工作树判存在：本地 fixture 的多个分支共用一个工作树，checkout 在 main
        时 pr-violation 的 evil.py 根本不在磁盘上。以 head_sha 的 tree 为准才不会漏判。
        """
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        for rel in self.python_files:
            raw = subprocess.run(
                ["git", "-C", self.repo, "show", f"{self.head_sha}:{rel}"],
                capture_output=True, timeout=60,
            )
            if raw.returncode != 0:
                raise RuntimeError(f"导出 {rel} 失败: {raw.stderr.decode(errors='replace')[:200]}")
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw.stdout)
        return out


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败 (exit={proc.returncode}): "
                           f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def is_git_repo(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def resolve_base(repo: Path, base: str | None = None) -> str | None:
    """按「显式参数 > 本地环境变量 > GitHub Actions 事件」的优先级找 base ref。"""
    env_base = os.environ.get("MW_PR_BASE")
    gh_base = os.environ.get("GITHUB_BASE_REF")
    candidates: list[str] = []
    if base:
        candidates.append(base)
    if env_base:
        candidates.append(env_base)
    if gh_base:
        candidates.extend((f"origin/{gh_base}", gh_base))
    for cand in candidates:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{cand}^{{commit}}"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return cand
    return None


def _parse_name_status(raw: str) -> tuple[ChangedFile, ...]:
    """解析 `git diff --name-status -M -z` 的 NUL 分隔输出。

    A/M/D/T/U: [status, path]；R/C: [status-with-score, old, new]。
    用 -z 的原因：文件名可能含空格/换行；按行 split 会把它们拆坏。
    """
    parts = raw.split("\0")
    out: list[ChangedFile] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        kind = status[:1]
        if kind in ("R", "C"):
            if i + 2 >= len(parts):
                break
            old, new = parts[i + 1], parts[i + 2]
            out.append(ChangedFile(status=status, path=new, old_path=old))
            i += 3
        else:
            if i + 1 >= len(parts):
                break
            out.append(ChangedFile(status=status, path=parts[i + 1]))
            i += 2
    return tuple(out)


def export_tree(repo: Path, ref: str, out_dir: Path,
                suffixes: tuple[str, ...] = (".py",)) -> tuple[tuple[str, ...], Path]:
    """把某个 commit 的全量文件导出，用于「全量 vs 增量」对照实验。"""
    repo = Path(repo).resolve()
    sha = _git(repo, "rev-parse", f"{ref}^{{commit}}").strip()
    tree = tuple(p for p in
                 _git(repo, "ls-tree", "-r", "--name-only", "-z", sha).split("\0")
                 if p and p.endswith(suffixes))
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for rel in tree:
        raw = subprocess.run(
            ["git", "-C", str(repo), "show", f"{sha}:{rel}"],
            capture_output=True, timeout=60,
        )
        if raw.returncode != 0:
            raise RuntimeError(f"导出 {rel} 失败")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.stdout)
    return tree, out


def read_pr_diff(repo: Path, *, base: str | None = None,
                head: str | None = None, suffixes: tuple[str, ...] = (".py",)) -> PRDiff | None:
    """读一次真实三方 diff。不是 git 仓库 / 找不到 base -> None（调用方回退全量）。"""
    repo = Path(repo).resolve()
    if not is_git_repo(repo):
        return None
    base_ref = resolve_base(repo, base)
    if not base_ref:
        return None
    head_ref = head or os.environ.get("MW_PR_HEAD") or "HEAD"
    try:
        base_sha = _git(repo, "rev-parse", f"{base_ref}^{{commit}}").strip()
        head_sha = _git(repo, "rev-parse", f"{head_ref}^{{commit}}").strip()
        merge_base = _git(repo, "merge-base", base_sha, head_sha).strip()
        raw = _git(repo, "diff", "--name-status", "-M", "-z", merge_base, head_sha)
        tree_raw = _git(repo, "ls-tree", "-r", "--name-only", "-z", head_sha)
    except RuntimeError:
        return None
    changed = _parse_name_status(raw)
    tree = tuple(p for p in tree_raw.split("\0") if p)
    files = tuple(sorted(
        c.path for c in changed
        if not c.status.startswith("D")
        and c.path.endswith(suffixes)
        and c.path in tree
    ))
    label = head_sha[:12] if head_ref == "HEAD" else head_ref
    return PRDiff(
        repo=str(repo), base_ref=base_ref, head_ref=label,
        base_sha=base_sha, head_sha=head_sha, merge_base=merge_base,
        changed=changed, python_files=files, tree_files=tree,
        source=f"git-diff {base_ref}...{label} @ {merge_base[:12]}",
    )
