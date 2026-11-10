"""代码级变异测试：证明每条门禁真的会开火，而不是「写了就算有」。

P0 的 mutation_test 只是字符串替换，验证的是谓词本身；P2 换成**真实应用的代码变异**：
在干净 worktree 上故意改坏 pricing.py / cli.py，然后真的跑 Drive 段（启动 CLI、
走用户路径、核验副作用）。门禁必须在每个坏版本上失败，否则规则形同虚设。

这直接对应架构文档 §L6 与 nicolasmelo1/software-factory 的「规则双写 + 变异证明」，
也是「A plateau is not a stop… never relax the predicate to declare victory」的落地。
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from ..workspace.seed import CLI_SOURCE, PRICING_CORRECT, GitWorkspace
from .generator import GeneratedSkill
from .runner import drive_probe

# (名称, 目标文件, 原文, 破坏后文本) —— 每条对应一个被门禁覆盖的失败模式。
MUTATIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "wrong_tax_rate", "shop/pricing.py",
        "TAX_RATE = 0.06", "TAX_RATE = 0.10",
    ),
    (
        "missing_total_prefix", "shop/cli.py",
        'line = f"TOTAL={format_amount(total_price(items))}"',
        'line = format_amount(total_price(items))',
    ),
    (
        "no_receipt_side_effect", "shop/cli.py",
        '        (data_dir / "receipt.json").write_text(\n'
        '            json.dumps({"items": items, "total": line}, ensure_ascii=False),\n'
        '            encoding="utf-8",\n'
        '        )',
        '        pass  # mutant: 副作用消失',
    ),
    (
        "no_rounding", "shop/pricing.py",
        'return f"{value:.2f}"', 'return f"{value}"',
    ),
    (
        "receipt_mismatch", "shop/cli.py",
        'json.dumps({"items": items, "total": line}, ensure_ascii=False)',
        'json.dumps({"items": items, "total": "TOTAL=0.00"}, ensure_ascii=False)',
    ),
)

# 多文件场景额外一条：doctor 不再报税率。多出来的那条 Drive 断言
# 必须也有变异体证明它会开火 —— 否则这条断言就是「写了没人验」。
MUTATIONS_MULTI: tuple[tuple[str, str, str, str], ...] = MUTATIONS + (
    (
        "doctor_missing_rate", "shop/cli.py",
        'print(f"SHOP_READY version={VERSION} rate={TAX_RATE:.2f}")',
        'print(f"SHOP_READY version={VERSION}")',
    ),
)


@dataclass(frozen=True, slots=True)
class MutationCase:
    name: str
    target: str
    baseline_ok: bool
    drove: bool
    fired: bool
    stdout: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MutationReport:
    baseline_green: bool
    cases: tuple[MutationCase, ...]
    all_fired: bool
    report_hash: str

    def to_dict(self) -> dict:
        return {
            "baseline_green": self.baseline_green,
            "cases": [c.to_dict() for c in self.cases],
            "all_fired": self.all_fired,
            "report_hash": self.report_hash,
        }


def _apply(tree, relpath: str, old: str, new: str) -> None:
    current = tree.read_file(relpath)
    if old not in current:
        raise RuntimeError(f"变异位点失效（源码已变）: {relpath} :: {old[:50]!r}")
    tree.write_file(relpath, current.replace(old, new, 1))


def _clear_bytecode(tree) -> None:
    """删掉树里的 __pycache__。

    这是「陈旧字节码」陷阱的第二道防线：第一道是 helper 里的
    PYTHONDONTWRITEBYTECODE=1（根本不写 .pyc）。这里再把**已经存在的**
    .pyc 清掉 —— 万一之前有别的运行写过，同长度改动 + 秒级 mtime 会让
    Python 复用陈旧字节码，导致「故意改坏的版本根本没被加载」，
    变异门禁就会静默失效（这正是真实 CI 上抓到的那次）。
    """
    for cache in tree.path.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)


def _probe(tree, skill: GeneratedSkill, run_root: Path,
           label: str, mutations: tuple[tuple[str, str, str, str], ...] = (),
           baseline_files: dict[str, str] | None = None) -> tuple[bool, str, bool]:
    """每个变体从**内容干净**的基线重新写起，等价于一份干净副本。

    这里刻意只用一个 worktree：变异测试串行执行，逐次重写两个文件即可保证
    基线不被上一个变体污染；真正的「干净环境」边界在 verifier 的独立 worktree
    （git apply 重放作者 diff），不需要为每个变体再付一次 git worktree add 的代价。

    `baseline_files` 必须与 skill 的期望一致：多文件场景的干净基线是
    「两个文件都改对」的状态，否则基线本身就不绿，变异门禁会误判。
    """
    for rel, content in (baseline_files or {
        "shop/pricing.py": PRICING_CORRECT, "shop/cli.py": CLI_SOURCE,
    }).items():
        tree.write_file(rel, content)
    for _name, relpath, old, new in mutations:
        _apply(tree, relpath, old, new)
    _clear_bytecode(tree)
    run_dir = run_root / f"{label}-{uuid.uuid4().hex[:8]}"
    ok, phases, executed = drive_probe(skill, tree.path, run_dir)
    drive = next((p for p in phases if p.phase == "drive"), None)
    return ok, (drive.stdout if drive else "drive 未执行")[:300], executed


def run_mutation_suite(
    skill: GeneratedSkill,
    workspace: GitWorkspace,
    run_root: Path,
    *,
    baseline_files: dict[str, str] | None = None,
    mutations: tuple[tuple[str, str, str, str], ...] = MUTATIONS,
) -> MutationReport:
    run_root.mkdir(parents=True, exist_ok=True)
    tree = workspace.worktree("mutations")
    baseline_green, baseline_out, _ = _probe(
        tree, skill, run_root, "baseline", baseline_files=baseline_files)

    cases: list[MutationCase] = []
    for name, relpath, old, new in mutations:
        ok, out, executed = _probe(tree, skill, run_root, name,
                                   ((name, relpath, old, new),),
                                   baseline_files=baseline_files)
        cases.append(MutationCase(
            name=name, target=relpath,
            baseline_ok=baseline_green,
            drove=executed,
            # 只有 Drive 真的跑过、且在坏版本上失败，才算门禁开火。
            fired=executed and not ok,
            stdout=out,
        ))

    all_fired = baseline_green and all(c.fired for c in cases)
    payload = {
        "skill_hash": skill.skill_hash,
        "baseline_green": baseline_green,
        "baseline_stdout": baseline_out,
        "cases": [c.to_dict() for c in cases],
    }
    return MutationReport(
        baseline_green=baseline_green,
        cases=tuple(cases),
        all_fired=all_fired,
        report_hash=content_hash(payload),
    )
