#!/usr/bin/env python3
"""可复现的「真实 git 仓库」靶场：不是 zip、不是伪造的 diff 文本，是真的 commit / branch。

四个分支覆盖 PR 门禁最容易写错的情形：
* `main`            —— 基线，含一个 **历史遗留** 的 eval 违规文件 legacy.py
* `pr-clean`        —— 只改 README + 新增干净 .py（增量扫描应绿）
* `pr-violation`    —— 新增含 eval 的 evil.py（增量扫描必须红，且精确命中该文件/规则）
* `pr-rename`       —— 重命名 app.py + 删除 legacy.py（R/D 状态解析不能崩）

legacy.py 是关键：它证明门禁按 **增量** 而非全量裁决 —— 老违规没被本 PR 碰过就
不该让这个 PR 变红，否则任何历史仓库都没法渐进收敛。gate 里同时给出「全量扫会红」的对照。

commit 时间 / 作者全部固定，同一个 tree 每次生成得到同一个 SHA，报告可逐字对比。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FIXED_DATE = "2026-11-10T00:00:00+00:00"
BRANCHES = ("main", "pr-clean", "pr-violation", "pr-rename")

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "michelin-gate",
    "GIT_AUTHOR_EMAIL": "gate@michelin.local",
    "GIT_COMMITTER_NAME": "michelin-gate",
    "GIT_COMMITTER_EMAIL": "gate@michelin.local",
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
}

LEGACY = '''"""历史遗留：这个违规在 main 上就存在，本 PR 没碰它就不得拦本 PR。"""


def old_total(value):
    return eval("1 + 1") + value
'''

APP = '''"""干净的业务模块。"""


def total(a, b):
    return a + b
'''

EVIL = '''"""本次 PR 新引入的违规：必须被增量门禁拦下。"""


def compute(value):
    return eval("2 + 2") + value
'''

FEATURE = '''"""干净的新功能。"""


def double(value):
    return value * 2
'''


@dataclass(frozen=True, slots=True)
class DemoRepo:
    root: Path
    shas: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"root": str(self.root), "branches": list(BRANCHES), "shas": dict(self.shas)}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        env=GIT_ENV, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fixture git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def _branch_list(root: Path) -> set[str]:
    return set(_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines())


def ensure_demo_repo(root: Path) -> DemoRepo:
    """幂等：已有完整仓库直接复用；残缺仓库一律报错，不静默重建、不删用户文件。"""
    root = Path(root).resolve()
    if (root / ".git").is_dir():
        missing = set(BRANCHES) - _branch_list(root)
        if missing:
            raise RuntimeError(f"PR fixture 仓库分支残缺 {sorted(missing)}，请移走 {root} 后重跑")
        return DemoRepo(root, {b: _git(root, "rev-parse", b) for b in BRANCHES})

    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"{root} 已存在且非空，拒绝覆盖；请移走后重跑")
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")

    def put(name: str, text: str) -> None:
        (root / name).write_text(text, encoding="utf-8")

    put("README.md", "# PR gate demo\n\n基线 v1。\n")
    put("app.py", '"""基线模块：干净。"""\n\n\ndef add(a, b):\n    return a + b\n')
    put("legacy.py", LEGACY)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline: 含历史遗留违规")

    _git(root, "checkout", "-q", "-b", "pr-clean")
    put("README.md", "# PR gate demo\n\n只改文档，不引入违规。\n")
    put("feature.py", FEATURE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "docs + clean feature")

    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "pr-violation")
    put("evil.py", EVIL)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "introduce forbidden eval")

    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "pr-rename")
    _git(root, "mv", "app.py", "renamed.py")
    _git(root, "rm", "-q", "legacy.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "rename app, delete legacy")

    _git(root, "checkout", "-q", "main")
    shas = {b: _git(root, "rev-parse", b) for b in BRANCHES}
    return DemoRepo(root, shas)
