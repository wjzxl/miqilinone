"""P3 规则变异证明：每条结构化规则都必须被「故意违规」证明会开火。

与 P2 的差别：P2 变异的是应用业务代码（修错税率），P3 变异的是**规则本身** ——
往仓库副本里注入一段真实违规代码，证明：
1. 规则引擎（python-ast / ast-grep）确实报出这条规则；
2. 生成的 verify skill 的 Drive 段确实因此 DRIVE_FAIL（门禁接到了执行体，不是纸面规则）。

只做第 1 点等于规则库自测，做满两点才能说明「生成的 skill 真的会拦住这次改动」。

所有变异都在**临时仓库副本**里进行，真实仓库从头到尾不被写入。
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from .generic_skill import GeneratedGenericSkill
from .plan import RuleMutation, VerificationPlan
from .rules import BACKENDS, RuleScan, scan
from .runner import drive_probe

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "data", ".workbuddy", "*.db", "*.pyc",
)


@dataclass(frozen=True, slots=True)
class RuleMutationCase:
    rule_id: str
    inject_file: str
    rule_fired: bool
    drive_rejected: bool
    backend: str
    scan_findings: int
    drive_stdout: str

    @property
    def fired(self) -> bool:
        return self.rule_fired and self.drive_rejected

    def to_dict(self) -> dict:
        return {**asdict(self), "fired": self.fired}


@dataclass(frozen=True, slots=True)
class RuleMutationReport:
    baseline_clean: bool
    baseline_drive_ok: bool
    backend: str
    cases: tuple[RuleMutationCase, ...]
    all_fired: bool
    report_hash: str
    shadow_root: str

    def to_dict(self) -> dict:
        return {
            "baseline_clean": self.baseline_clean,
            "baseline_drive_ok": self.baseline_drive_ok,
            "backend": self.backend,
            "cases": [c.to_dict() for c in self.cases],
            "all_fired": self.all_fired,
            "report_hash": self.report_hash,
            "note": "每条规则都注入真实违规代码；规则命中且生成 skill 的 Drive 拒绝，才算开火",
        }


class ShadowRepo:
    """目标仓库的一次性副本；注入后 restore，绝不写回真实仓库。"""

    def __init__(self, source: Path):
        self.source = Path(source).resolve()
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-shadow-"))
        self.root = self.tmp / "repo"
        shutil.copytree(self.source, self.root, ignore=COPY_IGNORE)
        self._originals: dict[str, str] = {}

    def inject(self, rel: str, snippet: str) -> None:
        path = self.root / rel
        if not path.is_file():
            raise FileNotFoundError(f"变异目标不存在：{rel}")
        if rel not in self._originals:
            self._originals[rel] = path.read_text(encoding="utf-8")
        path.write_text(self._originals[rel].rstrip() + "\n\n" + snippet.strip() + "\n",
                        encoding="utf-8")

    def restore(self) -> None:
        for rel, text in self._originals.items():
            (self.root / rel).write_text(text, encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "ShadowRepo":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _rule_hit(scan_result: RuleScan, rule_id: str) -> bool:
    return any(f.rule_id == rule_id for f in scan_result.findings)


def run_rule_mutations(
    skill: GeneratedGenericSkill,
    plan: VerificationPlan,
    repo_root: Path,
    run_root: Path,
    *,
    backend: str = "python-ast",
    executable: str | None = None,
    probe_drive: bool = True,
) -> RuleMutationReport:
    if backend not in BACKENDS:
        raise ValueError(f"未知后端 {backend!r}，可用 {BACKENDS}")
    run_root.mkdir(parents=True, exist_ok=True)
    rules = plan.rules
    cases: list[RuleMutationCase] = []

    with ShadowRepo(repo_root) as shadow:
        baseline_scan = scan(rules, shadow.root, backend, executable=executable)
        baseline_drive_ok = True
        if probe_drive:
            ok, phases, executed = drive_probe(
                skill, shadow.root, run_root / f"baseline-{uuid.uuid4().hex[:8]}"
            )
            drive = next((p for p in phases if p.phase == "drive"), None)
            baseline_drive_ok = bool(ok and executed)
            baseline_out = (drive.stdout if drive else "drive 未执行")[:300]
        else:
            baseline_out = "skipped"

        for m in plan.mutations:
            shadow.inject(m.inject_file, m.snippet)
            hit_scan = scan(rules, shadow.root, backend, executable=executable)
            rule_fired = _rule_hit(hit_scan, m.rule_id)
            drive_rejected = True
            drive_out = "skipped"
            if probe_drive:
                ok, phases, executed = drive_probe(
                    skill, shadow.root, run_root / (m.rule_id + "-" + uuid.uuid4().hex[:8])
                )
                drive = next((p for p in phases if p.phase == "drive"), None)
                # 规则命中 → Drive 必须拒绝；且必须是 Drive 真的跑过之后的拒绝。
                drive_rejected = bool(executed and not ok and drive
                                      and "DRIVE_FAIL" in drive.stdout)
                drive_out = (drive.stdout if drive else "drive 未执行")[:300]
            cases.append(RuleMutationCase(
                rule_id=m.rule_id,
                inject_file=m.inject_file,
                rule_fired=rule_fired,
                drive_rejected=drive_rejected,
                backend=backend,
                scan_findings=len(hit_scan.findings),
                drive_stdout=drive_out,
            ))
            shadow.restore()

        all_fired = (baseline_scan.clean and baseline_drive_ok
                     and bool(cases) and all(c.fired for c in cases))
        payload = {
            "skill_hash": skill.skill_hash,
            "plan": plan.fingerprint(),
            "backend": backend,
            "baseline_clean": baseline_scan.clean,
            "baseline_drive_ok": baseline_drive_ok,
            "baseline_stdout": baseline_out if probe_drive else "",
            "cases": [c.to_dict() for c in cases],
        }
        return RuleMutationReport(
            baseline_clean=baseline_scan.clean,
            baseline_drive_ok=baseline_drive_ok,
            backend=backend,
            cases=tuple(cases),
            all_fired=all_fired,
            report_hash=content_hash(payload),
            shadow_root=str(shadow.root),
        )
