"""六段式执行器：把生成的 skill 真的跑一遍，而不是只检查它写得像不像。

pstack §4 的四条硬约束在这里逐条落地：
1. "Run its own instructions end to end once" —— launch/doctor/drive/evidence/cleanup/helpers 全部 subprocess 实跑。
2. "run the generated cleanup after every failed iteration too" —— 任何一段失败也要跑 cleanup，
   否则坏掉的尝试会遗留进程、端口和 scratch 目录。
3. "After cleanup, confirm the evidence still exists at the named location —
   a cleanup that eats the proof fails this step." —— 显式 evidence_survived 检查。
4. "A generated skill that was never executed is a draft, not a deliverable." ——
   executed=False 的 SkillProof 状态恒为 DRAFT，BehaviorVerifier 会拒绝用它裁决。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from .generator import GeneratedSkill
from .skill_spec import SIX_PHASES, PhaseResult, SkillProof

HELPER_REL = "helpers/verify_helper.py"


def _run_phase(skill: GeneratedSkill, app_root: Path, run_dir: Path, phase: str,
               *, extra_args: tuple[str, ...] = ()) -> PhaseResult:
    helper = skill.root / HELPER_REL
    env = dict(os.environ)
    env["VERIFY_APP_ROOT"] = str(Path(app_root).resolve())
    env["VERIFY_RUN_DIR"] = str(Path(run_dir).resolve())
    env.pop("VERIFY_EATS_EVIDENCE", None)
    command = f"{sys.executable} {HELPER_REL} {phase}"
    # -S 跳过 site 初始化：生成的 helper 只用标准库，本机 Python 启动从 ~0.42s
    # 降到 ~0.10s；一次 P2 全链路要起几十个子进程，这个差别很可观。
    proc = subprocess.run(
        [sys.executable, "-S", str(helper), phase, *extra_args],
        cwd=str(skill.root), env=env, capture_output=True, text=True, timeout=90,
    )
    return PhaseResult(
        phase=phase, returncode=proc.returncode,
        stdout=proc.stdout.strip(), stderr=proc.stderr.strip(),
        command=command,
    )


def _evidence_hash(run_dir: Path) -> str | None:
    proof = Path(run_dir) / "evidence" / "proof.json"
    if not proof.exists():
        return None
    return hashlib.sha256(proof.read_bytes()).hexdigest()


def run_phases(
    skill: GeneratedSkill,
    app_root: Path,
    run_dir: Path,
    phases: tuple[str, ...],
    *,
    cleanup_on_failure: bool = True,
    cleanup_args: tuple[str, ...] = (),
) -> tuple[PhaseResult, ...]:
    """按给定顺序执行若干段；失败时补跑 cleanup（不重复跑成功路径里的 cleanup）。"""
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    results: list[PhaseResult] = []
    for phase in phases:
        extra = cleanup_args if phase == "cleanup" else ()
        results.append(_run_phase(skill, app_root, run_dir, phase, extra_args=extra))
        if results[-1].returncode != 0:
            already_cleaned = any(p.phase == "cleanup" for p in results)
            if cleanup_on_failure and not already_cleaned:
                results.append(_run_phase(skill, app_root, run_dir, "cleanup",
                                          extra_args=cleanup_args))
            break
    return tuple(results)


def prove_skill(
    skill: GeneratedSkill,
    app_root: Path,
    run_dir: Path,
    *,
    stand_in_steps: tuple[str, ...] = (),
) -> SkillProof:
    """端到端跑一次生成的 skill；返回 PROVEN / FAILED / DRAFT 三态之一。"""
    phases = run_phases(skill, app_root, run_dir, SIX_PHASES)
    by_name = {p.phase: p for p in phases}

    proof_path = Path(run_dir) / "evidence" / "proof.json"
    evidence_survived = proof_path.exists() and proof_path.stat().st_size > 0
    reason = ""

    failed = [p for p in phases if p.returncode != 0]
    if failed:
        reason = "; ".join(f"{p.phase}: {p.stdout or p.stderr}" for p in failed)
    elif len(by_name) != len(SIX_PHASES):
        reason = "六段未全部执行：只跑了 " + ",".join(sorted(by_name))
    elif not evidence_survived:
        reason = "cleanup 吃掉了 evidence/proof.json —— a cleanup that eats the proof fails this step"
    elif "DRIVE_OK" not in by_name["drive"].stdout:
        reason = "drive 未驱动真实用户路径"

    executed = len(phases) > 0
    status = "DRAFT" if not executed else ("PROVEN" if not reason else "FAILED")
    return SkillProof(
        skill_hash=skill.skill_hash,
        executed=executed,
        status=status,
        phases=phases,
        evidence_survived=evidence_survived,
        evidence_hash=_evidence_hash(run_dir),
        stand_in_steps=tuple(stand_in_steps),
        failure_reason=reason,
    )


def drive_probe(
    skill: GeneratedSkill, app_root: Path, run_dir: Path, *, eats_evidence: bool = False
) -> tuple[bool, tuple[PhaseResult, ...], bool]:
    """变异测试用的轻量探针：drive → cleanup。

    变异测试只判断「Drive 的断言在这个坏版本上会不会开火」，六段式的完整
    Launch/Doctor/Evidence/Helpers 已在 prove_skill 里跑过一次；这里每个变体
    单独建实例没有额外信息，只会拖慢几十个子进程。
    CLI 自己会创建 SHOP_DATA_DIR 并写 receipt，因此 Drive 可独立运行。

    返回 (drive_ok, phases, drive_executed)。第三个值仍保留显式语义：
    Drive 没真正执行过就不能算「门禁开火」。
    """
    extra = ("--eats-evidence",) if eats_evidence else ()
    phases = run_phases(
        skill, app_root, run_dir, ("drive", "cleanup"),
        cleanup_args=extra,
    )
    drive = next((p for p in phases if p.phase == "drive"), None)
    executed = drive is not None
    ok = bool(executed and drive.returncode == 0 and "DRIVE_OK" in drive.stdout)
    return ok, phases, executed
