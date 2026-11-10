"""六段式 verify skill 的数据契约（对齐 pstack create-verification-skill/SKILL.md，MIT）。

一手核实（2026-11-09，raw 源码直取）的硬性约束，逐条在本模块可检查：

* 六个固定段落 Launch / Doctor / Drive / Evidence / Cleanup / Helpers，
  每段必须「grounded in what the interview actually found (no placeholders left)」。
* Launch: "Ready means usable, not just that a process answers."
* Doctor: "one read-only check that answers 'is this instance worth driving?'"
* Drive: "real selectors/commands from this repo, not examples"；
  Evidence: "exercise the real user path, not internal setters or test-only endpoints"
  且 "capture the action and the resulting state" + "verify side effects"。
* Cleanup: "Never kill by process name; kill what you started." /
  "Cleanup removes instances and scratch state, never the evidence"。
* Helpers: "any script the skill ships is executable and its invocation is shown in the skill body."
* §4: "A generated skill that was never executed is a draft, not a deliverable."
  且 "After cleanup, confirm the evidence still exists at the named location —
  a cleanup that eats the proof fails this step."
* §3 feature map: features/README.md + 每 feature 一个文件，四个固定 H2。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

SIX_PHASES: tuple[str, ...] = ("launch", "doctor", "drive", "evidence", "cleanup", "helpers")

FEATURE_MAP_H2: tuple[str, ...] = (
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with python3 -m shop.cli",
    "Gotchas",
)

EXPECTED_TOTAL = "TOTAL=31.80"
PROBE_ARGS: tuple[str, ...] = ("total", "10", "20")


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """一次「访谈仓库」的结果：surface / run / drive / observe / isolate。"""

    app_name: str
    surface: str
    run_command: str
    drive_command: str
    expected_output: str
    evidence_path: str
    isolate_note: str
    features: tuple[str, ...]

    def to_dict(self) -> dict:
        return {**asdict(self), "features": list(self.features)}


@dataclass(frozen=True, slots=True)
class PhaseResult:
    phase: str
    returncode: int
    stdout: str
    stderr: str
    command: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkillProof:
    """§4 的 prove 结果。executed=False 即 DRAFT，不得启用、不得用于裁决。"""

    skill_hash: str
    executed: bool
    status: str                 # DRAFT | PROVEN | FAILED
    phases: tuple[PhaseResult, ...]
    evidence_survived: bool
    evidence_hash: str | None
    stand_in_steps: tuple[str, ...]
    failure_reason: str

    @property
    def usable(self) -> bool:
        return self.status == "PROVEN"

    def to_dict(self) -> dict:
        return {
            "skill_hash": self.skill_hash,
            "executed": self.executed,
            "status": self.status,
            "phases": [p.to_dict() for p in self.phases],
            "evidence_survived": self.evidence_survived,
            "evidence_hash": self.evidence_hash,
            "stand_in_steps": list(self.stand_in_steps),
            "failure_reason": self.failure_reason,
        }


def build_shop_spec() -> SkillSpec:
    """P2 垂直切片的仓库访谈结果：一个真实 CLI，而不是占位符。"""
    return SkillSpec(
        app_name="shop",
        surface="CLI (python3 -m shop.cli)",
        run_command="python3 -m shop.cli doctor",
        drive_command="python3 -m shop.cli " + " ".join(PROBE_ARGS),
        expected_output=EXPECTED_TOTAL,
        evidence_path="evidence/proof.json （run 目录内，cleanup 后必须存活）",
        isolate_note="SHOP_DATA_DIR 按 run 隔离，两个实例可并行；绝不复用共享 data 目录",
        features=("total", "doctor", "receipt-side-effect"),
    )
