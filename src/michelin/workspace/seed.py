"""P2 真实 git 工作区：种子仓库 + git worktree 隔离 + unified diff / apply patch。

架构文档 §L3 明确 git worktree 只解决并行写冲突、零隔离；P2 用它做「每个任务一个
干净环境」，真正的执行隔离留给 P3 的容器。这里刻意用真实 `git` 二进制，不自己
解析 patch —— verifier 用 `git apply` 在干净 worktree 上重放作者产出的 diff，
保证「验证的是产物，不是作者对产物的描述」。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..platform.envelope import content_hash

# 种子应用：一个真实可跑的 CLI。baseline 故意带一个 bug（税率 0.10，应为 0.06）。
PRICING_BUGGY = '''"""结算逻辑。baseline 版本：税率写错，shop.cli 的 total 会输出错误金额。"""

TAX_RATE = 0.10


def total_price(items):
    return sum(items) * (1 + TAX_RATE)


def format_amount(value):
    return f"{value:.2f}"
'''

PRICING_CORRECT = '''"""结算逻辑。正确版本：税率 6%，金额保留两位。"""

TAX_RATE = 0.06


def total_price(items):
    return sum(items) * (1 + TAX_RATE)


def format_amount(value):
    return f"{value:.2f}"
'''

CLI_SOURCE = '''"""shop 的真实用户入口：python3 -m shop.cli total 10 20。

Drive 段必须走这个入口（真实用户路径），不许 import 内部函数直接算。
receipt.json 是用户动作产生的副作用，Evidence 段必须核验它。
"""
import json
import os
import sys
from pathlib import Path

from .pricing import total_price, format_amount

VERSION = "shop-1.0"


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: total <amount>... | doctor", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "doctor":
        print(f"SHOP_READY version={VERSION}")
        return 0
    if cmd == "total":
        items = [float(x) for x in argv[1:]]
        if not items:
            print("TOTAL=0.00")
            return 0
        line = f"TOTAL={format_amount(total_price(items))}"
        print(line)
        data_dir = Path(os.environ.get("SHOP_DATA_DIR", "./data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "receipt.json").write_text(
            json.dumps({"items": items, "total": line}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

CLI_SOURCE_MULTI = '''"""shop 的真实用户入口（多文件任务版本）：python3 -m shop.cli total 10 20。

与 CLI_SOURCE 的差别只有一个：doctor 会报出当前税率。
多文件任务要改两个文件（pricing.py 的税率 + cli.py 的 doctor 输出），
所以 golden 版本必须同时具备这两个正确状态，否则变异门禁的基线就不对。
"""
import json
import os
import sys
from pathlib import Path

from .pricing import total_price, format_amount, TAX_RATE

VERSION = "shop-1.0"


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: total <amount>... | doctor", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "doctor":
        print(f"SHOP_READY version={VERSION} rate={TAX_RATE:.2f}")
        return 0
    if cmd == "total":
        items = [float(x) for x in argv[1:]]
        if not items:
            print("TOTAL=0.00")
            return 0
        line = f"TOTAL={format_amount(total_price(items))}"
        print(line)
        data_dir = Path(os.environ.get("SHOP_DATA_DIR", "./data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "receipt.json").write_text(
            json.dumps({"items": items, "total": line}, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''


INIT_SOURCE = '"""shop: 一个用于验证循环的真实小应用。"""\n'

README = """# shop

真实 CLI 应用，用于 P2 验证循环：

    python3 -m shop.cli doctor
    python3 -m shop.cli total 10 20     # 正确输出 TOTAL=31.80

`total` 会写 `SHOP_DATA_DIR/receipt.json`，这是 Evidence 段要核验的副作用。
"""


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=60, check=check,
    )


class Worktree:
    """一个从 baseline commit 检出的干净工作树。"""

    def __init__(self, owner: "GitWorkspace", path: Path, tag: str):
        self.owner = owner
        self.path = path
        self.tag = tag

    def diff(self, pathspec: str = "shop/pricing.py") -> str:
        return _git("-C", str(self.path), "diff", "--no-color", "--", pathspec).stdout

    def diff_all(self) -> str:
        """整个 worktree 相对 HEAD 的 diff（多文件编辑用）。

        仍然用真 git 生成：模型给的是「文件 → 新内容」，写盘与 diff 都是
        确定性的，验证的是产物而不是作者对产物的描述。
        """
        return _git("-C", str(self.path), "diff", "--no-color").stdout

    def head_sha(self) -> str:
        return _git("-C", str(self.path), "rev-parse", "HEAD").stdout.strip()

    def apply_patch(self, patch: str) -> None:
        """先在干净树上 --check，再 apply；patch 来自作者，属于不可信输入。"""
        if not patch.strip():
            raise ValueError("空 patch 不得进入验证")
        patch_file = self.path.parent / f"{self.tag}-{uuid.uuid4().hex[:8]}.patch"
        patch_file.write_text(patch, encoding="utf-8")
        try:
            checked = _git("-C", str(self.path), "apply", "--check", str(patch_file), check=False)
            if checked.returncode != 0:
                raise ValueError(f"patch 无法应用: {checked.stderr.strip()}")
            _git("-C", str(self.path), "apply", str(patch_file))
        finally:
            patch_file.unlink(missing_ok=True)

    def write_file(self, relpath: str, content: str) -> None:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_file(self, relpath: str) -> str:
        return (self.path / relpath).read_text(encoding="utf-8")


class GitWorkspace:
    """管理一个临时种子仓库与若干个 worktree；close() 只删自己创建的东西。"""

    def __init__(self, root: Path | None = None):
        self.root = (root or Path(tempfile.mkdtemp(prefix="mw-p2-repo-"))).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed = self.root / "seed"
        self._worktrees: list[tuple[str, Path]] = []
        self._seeded = False

    def baseline_fingerprint(self) -> str:
        """基线**内容**指纹，用于绑定 verdict。

        不能用 commit SHA：每次新建临时仓库时 commit 时间戳不同，同样的 diff
        会得到不同的 head_sha，裁决就无法复现了。内容才是「一次裁决覆盖一个基线」
        里真正要锁住的东西。
        """
        self.seed_baseline()
        files = {
            p.relative_to(self.seed).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(self.seed.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        }
        return content_hash(files)

    def seed_baseline(self) -> str:
        if self._seeded:
            return _git("-C", str(self.seed), "rev-parse", "HEAD").stdout.strip()
        self.seed.mkdir(parents=True, exist_ok=True)
        (self.seed / "shop").mkdir(parents=True, exist_ok=True)
        (self.seed / "shop" / "__init__.py").write_text(INIT_SOURCE, encoding="utf-8")
        (self.seed / "shop" / "pricing.py").write_text(PRICING_BUGGY, encoding="utf-8")
        (self.seed / "shop" / "cli.py").write_text(CLI_SOURCE, encoding="utf-8")
        (self.seed / "README.md").write_text(README, encoding="utf-8")
        _git("init", "-q", "-b", "main", str(self.seed))
        _git("-C", str(self.seed), "config", "user.email", "workbench@local")
        _git("-C", str(self.seed), "config", "user.name", "Michelin Workbench")
        _git("-C", str(self.seed), "add", "-A")
        _git("-C", str(self.seed), "commit", "-q", "-m", "baseline: buggy tax rate")
        self._seeded = True
        return _git("-C", str(self.seed), "rev-parse", "HEAD").stdout.strip()

    def worktree(self, tag: str | None = None) -> Worktree:
        self.seed_baseline()
        tag = tag or uuid.uuid4().hex[:10]
        branch = f"wt-{tag}-{uuid.uuid4().hex[:6]}"
        path = self.root / f"tree-{tag}-{uuid.uuid4().hex[:6]}"
        _git("-C", str(self.seed), "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
        self._worktrees.append((branch, path))
        return Worktree(self, path, tag)

    def close(self) -> None:
        for branch, path in reversed(self._worktrees):
            _git("-C", str(self.seed), "worktree", "remove", "--force", str(path), check=False)
            _git("-C", str(self.seed), "branch", "-D", branch, check=False)
        self._worktrees.clear()
        shutil.rmtree(self.root, ignore_errors=True)
