"""把 SkillSpec 编译成一份**可执行**的 project-local verification skill。

对齐 pstack create-verification-skill §2–§3：
* 产出 `.cursor/skills/verify-<app>/SKILL.md`，YAML frontmatter + 六个固定段落，
  每段 grounded in the interview（真实命令，无 placeholder）。
* Helpers 段落要求的脚本必须可执行（chmod 755），且调用命令写在 SKILL.md 正文里。
* feature map：features/README.md + 每个用户可见功能一个文件，四个固定 H2。
* 生成物整体做 content_hash —— verdict 要绑定到 skill_hash，改了门禁就得重裁。

生成的不是给人看的说明书，是给「冷启动、从没见过这个 app 的 agent」读的操作手册。
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from .skill_spec import (
    EXPECTED_TOTAL, FEATURE_MAP_H2, PROBE_ARGS, SIX_PHASES, SkillSpec, build_shop_spec,
)

HELPER_TEMPLATE = r'''#!/usr/bin/env python3
"""verify-shop helper：六段式执行体，由生成器产出，可被冷启动 agent 直接执行。

环境变量：VERIFY_APP_ROOT（应用仓库）、VERIFY_RUN_DIR（本次 run 的隔离目录）。
Drive 段走真实用户入口 python3 -m shop.cli，不 import 内部函数、不用 test-only 端点。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_MODULE = "shop.cli"
EXPECTED_TOTAL = "__EXPECTED_TOTAL__"
PROBE_ARGS = __PROBE_ARGS__


def root() -> Path:
    return Path(os.environ["VERIFY_APP_ROOT"]).resolve()


def run_dir() -> Path:
    return Path(os.environ["VERIFY_RUN_DIR"]).resolve()


def instance_dir() -> Path:
    return run_dir() / "instance"


def evidence_dir() -> Path:
    return run_dir() / "evidence"


def cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["SHOP_DATA_DIR"] = str(instance_dir() / "data")
    env["PYTHONPATH"] = str(root())
    return subprocess.run(
        [sys.executable, "-S", "-m", APP_MODULE, *args],
        cwd=str(root()), env=env, capture_output=True, text=True, timeout=30,
    )


def phase_launch() -> int:
    # Launch: 从空数据目录达到 usable 状态。ready 不是「进程有响应」，是能算出探针值。
    (instance_dir() / "data").mkdir(parents=True, exist_ok=True)
    (instance_dir() / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")
    doctor = cli("doctor")
    if doctor.returncode != 0 or "SHOP_READY" not in doctor.stdout:
        print("LAUNCH_FAIL not ready: " + doctor.stdout.strip() + doctor.stderr.strip())
        return 1
    probe = cli("total", "1", "2")
    # 只证明「计算通路可用」，不代入业务输出断言 —— 那是 Drive 的职责。
    # 否则输出格式类 mutant 会在 Launch 就挂掉，Drive 根本没机会执行。
    if probe.returncode != 0 or not probe.stdout.strip():
        print("LAUNCH_FAIL process answered but app is not usable")
        return 1
    print("LAUNCH_OK ready=usable data_dir=" + str(instance_dir() / "data")
          + " probe=" + probe.stdout.strip())
    return 0


def phase_doctor() -> int:
    # Doctor: 只读体检，不写任何文件。"is this instance worth driving?"
    marker = instance_dir() / "owner.pid"
    if not marker.exists():
        print("DOCTOR_FAIL instance not launched by this run")
        return 1
    if not (instance_dir() / "data").is_dir():
        print("DOCTOR_FAIL isolated data dir missing")
        return 1
    r = cli("doctor")
    if r.returncode != 0 or "SHOP_READY version=shop-1.0" not in r.stdout:
        print("DOCTOR_FAIL wrong build or unhealthy: " + r.stdout.strip() + r.stderr.strip())
        return 1
    print("DOCTOR_OK build=shop-1.0 isolated_data=true read_only=true")
    return 0


def phase_drive() -> int:
    # Drive: 真实用户路径，动作 + 结果状态成对，并核验副作用。
    r = cli(*PROBE_ARGS)
    stdout = r.stdout.strip()
    receipt = instance_dir() / "data" / "receipt.json"
__DRIVE_CHECKS__
    proof = {
        "action": "python3 -m " + APP_MODULE + " " + " ".join(PROBE_ARGS),
        "command_exit_code": r.returncode,
        "resulting_state": stdout,
        "side_effect": {
            "path": "receipt.json",
            "exists": receipt.exists(),
            "total": (json.loads(receipt.read_text(encoding="utf-8")).get("total")
                      if receipt.exists() else None),
        },
        "stderr": r.stderr.strip(),
    }
    evidence_dir().mkdir(parents=True, exist_ok=True)
    (evidence_dir() / "proof.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("DRIVE_OK " + stdout + " evidence=evidence/proof.json")
    return 0


def phase_evidence() -> int:
    p = evidence_dir() / "proof.json"
    if not p.exists():
        print("EVIDENCE_FAIL proof.json missing")
        return 1
    d = json.loads(p.read_text(encoding="utf-8"))
    required = ("action", "command_exit_code", "resulting_state", "side_effect")
    missing = [k for k in required if k not in d]
    if missing:
        print("EVIDENCE_FAIL incomplete proof: " + ",".join(missing))
        return 1
    if not d["side_effect"].get("exists"):
        print("EVIDENCE_FAIL side effect not observed")
        return 1
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    print("EVIDENCE_OK action+state+side_effect captured sha256=" + digest[:16])
    return 0


def phase_cleanup() -> int:
    # Cleanup: 只删本次 run 创建的 instance/scratch；绝不按进程名 kill；绝不删证据。
__CLEANUP_BODY__
    print("CLEANUP_OK instance_removed=true evidence_preserved=true")
    return 0


def phase_helpers() -> int:
    # Helpers: 自检脚本可执行、且 SKILL.md 正文写明了每一段的确切调用。
    helper = Path(__file__)
    if not os.access(str(helper), os.X_OK):
        print("HELPERS_FAIL helper is not executable")
        return 1
    skill_md = helper.parent.parent / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    missing = [p for p in ("launch", "doctor", "drive", "evidence", "cleanup", "helpers")
               if ("verify_helper.py " + p) not in body]
    if missing:
        print("HELPERS_FAIL invocation not documented for: " + ",".join(missing))
        return 1
    print("HELPERS_OK executable=true invocations_documented=" + str(len(SIX_PHASES_LOCAL)))
    return 0


SIX_PHASES_LOCAL = ("launch", "doctor", "drive", "evidence", "cleanup", "helpers")
PHASES = {
    "launch": phase_launch,
    "doctor": phase_doctor,
    "drive": phase_drive,
    "evidence": phase_evidence,
    "cleanup": phase_cleanup,
    "helpers": phase_helpers,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--eats-evidence", action="store_true",
                    help="故意破坏：让 cleanup 删掉证据，用于证明 runner 会拒绝这种 skill")
    args = ap.parse_args()
    if args.eats_evidence:
        os.environ["VERIFY_EATS_EVIDENCE"] = "1"
    return PHASES[args.phase]()


if __name__ == "__main__":
    raise SystemExit(main())
'''

DRIVE_CHECKS_REAL = '''    if r.returncode != 0:
        print("DRIVE_FAIL command failed: " + r.stderr.strip())
        return 1
    if EXPECTED_TOTAL not in stdout:
        print("DRIVE_FAIL expected " + EXPECTED_TOTAL + " got " + repr(stdout))
        return 1
    if not receipt.exists():
        print("DRIVE_FAIL missing side effect: receipt.json")
        return 1
    try:
        recorded = json.loads(receipt.read_text(encoding="utf-8")).get("total")
    except (ValueError, OSError) as exc:
        print("DRIVE_FAIL unreadable receipt: " + str(exc))
        return 1
    if recorded != stdout:
        print("DRIVE_FAIL side effect mismatch: receipt=" + repr(recorded)
              + " stdout=" + repr(stdout))
        return 1'''

DRIVE_CHECKS_WEAKENED = '''    # 故意弱化：门禁形同虚设，变异测试必须能抓到这一点。
    pass'''

CLEANUP_BODY_REAL = '''    shutil.rmtree(instance_dir(), ignore_errors=True)
    if "VERIFY_EATS_EVIDENCE" in os.environ:
        shutil.rmtree(evidence_dir(), ignore_errors=True)'''

CLEANUP_BODY_EATS = '''    shutil.rmtree(instance_dir(), ignore_errors=True)
    shutil.rmtree(evidence_dir(), ignore_errors=True)'''

FEATURE_BODIES: dict[str, tuple[str, str, str, str]] = {
    "total": (
        "- 购物车金额合计与税额计算\n- 金额格式化为两位小数",
        "在终端执行 `python3 -m shop.cli total 10 20`，用户看到 `TOTAL=31.80`。",
        "`python3 helpers/verify_helper.py drive` 走同一入口，断言 stdout 为 `TOTAL=31.80`。",
        "不要直接 import `total_price()` 断言返回值：那是内部 setter，不是用户路径。"
        "税率写错时该 journey 必须报失败，绝不为了通过而放宽期望值。",
    ),
    "doctor": (
        "- 启动就绪探针\n- 版本与构建校验",
        "执行 `python3 -m shop.cli doctor`，看到 `SHOP_READY version=shop-1.0`。",
        "`python3 helpers/verify_helper.py launch` 与 `doctor` 分别负责启动与只读体检。",
        "Doctor 必须只读，不写任何文件；进程有响应不等于应用 usable，Launch 还要跑一次探针。",
    ),
    "receipt-side-effect": (
        "- 用户动作产生的 receipt.json\n- 落盘金额与 stdout 一致",
        "执行 `total` 后查看 `SHOP_DATA_DIR/receipt.json`。",
        "`drive` 在断言 stdout 之后立即读取 receipt，验证 `total` 字段与 stdout 一致；"
        "`evidence` 再核验 proof.json 里同时记录了 action 与 resulting state。",
        "SHOP_DATA_DIR 必须按 run 隔离，两个实例不能共享数据目录。"
        "Cleanup 删除 instance 与 scratch，但 evidence/proof.json 必须存活。",
    ),
}


@dataclass(frozen=True, slots=True)
class GeneratedSkill:
    root: Path
    skill_hash: str
    files: tuple[str, ...]
    spec: SkillSpec
    weakened_drive: bool
    cleanup_eats_evidence: bool


def _render_helper(*, weaken_drive: bool, eats_evidence: bool) -> str:
    text = HELPER_TEMPLATE
    text = text.replace("__EXPECTED_TOTAL__", EXPECTED_TOTAL)
    text = text.replace("__PROBE_ARGS__", repr(tuple(PROBE_ARGS)))
    text = text.replace("__DRIVE_CHECKS__", DRIVE_CHECKS_WEAKENED if weaken_drive else DRIVE_CHECKS_REAL)
    text = text.replace("__CLEANUP_BODY__", CLEANUP_BODY_EATS if eats_evidence else CLEANUP_BODY_REAL)
    leftovers = [t for t in (
        "__EXPECTED_TOTAL__", "__PROBE_ARGS__", "__DRIVE_CHECKS__", "__CLEANUP_BODY__"
    ) if t in text]
    if leftovers:
        raise ValueError(f"helper 模板存在未替换的占位符: {leftovers}")
    return text


def _render_skill_md(spec: SkillSpec) -> str:
    calls = "\n".join(
        f"    python3 helpers/verify_helper.py {phase}"
        for phase in SIX_PHASES
    )
    return f"""---
name: verify-{spec.app_name}
description: "Drive the {spec.app_name} {spec.surface} like a real user and capture proof. Use when verifying a change to {spec.app_name} before handing it over."
disable-model-invocation: true
---

# Verify {spec.app_name}

生成物由仓库访谈结果编译，所有命令都是本仓库的真实入口，没有占位符。
执行前设置：`VERIFY_APP_ROOT=<被验证的 worktree>`、`VERIFY_RUN_DIR=<本次 run 的隔离目录>`。

## Launch

    {spec.run_command}

`python3 helpers/verify_helper.py launch` 从空数据目录创建 `instance/data`，
跑 doctor 后再跑一次探针 `total 1 2`。**Ready means usable, not just that a process
answers** —— 数据库健康检查会通过但初始化脚本仍在跑的情况，这里用一个真实计算探针排除。
探针只验证计算通路（退出码 0、有输出），**不代入业务断言**：期望金额属于 Drive，
写进 Launch 会让输出格式类变体还没到 Drive 就被拦下，变异测试因此失真。
实例归属写在 `instance/owner.pid`，供 Cleanup 精确回收。

## Doctor

    python3 helpers/verify_helper.py doctor

一次只读检查：instance 由本 run 创建、隔离数据目录存在、构建版本为 `shop-1.0`。
不写任何文件；任何环节看着不对时，agent 先跑这一段再决定要不要继续 Drive。

## Drive

    {spec.drive_command}
    python3 helpers/verify_helper.py drive

真实用户路径：通过 CLI 入口下命令，断言 stdout 为 `{spec.expected_output}`，
并立刻读取副作用文件 `receipt.json` 核对金额。**不 import 内部函数、不用 test-only
端点**（pstack: "exercise the real user path, not internal setters or test-only endpoints"）。
动作与结果状态成对写入证据：{spec.evidence_path}。

## Evidence

    python3 helpers/verify_helper.py evidence

proof.json 必须同时含 action、resulting_state、side_effect、command_exit_code；
`side_effect.exists` 为真且 receipt 的 total 与 stdout 一致。只截最终屏幕不算证明。
隔离说明：{spec.isolate_note}。

## Cleanup

    python3 helpers/verify_helper.py cleanup

只删本 run 创建的 `instance/`（含 owner.pid 与 scratch data），**绝不按进程名 kill**，
**绝不删 evidence/**。Cleanup 之后证据必须仍在上面写明的路径；清理把证据吃掉即判定失败。

## Helpers

    {calls}

`helpers/verify_helper.py` 由生成器赋予可执行位（0755），六个子命令的调用全部写在
本文件正文里，冷启动 agent 不需要反向工程。`helpers` 子命令自检可执行位与本文档覆盖度。

## Prove before handover

按 launch → doctor → drive → evidence → cleanup → helpers 跑一遍，
cleanup 后确认 evidence/proof.json 仍存在。从未执行过的 skill 只是 draft，不得用于裁决。
feature map 见 `features/README.md`；至少驱动其中一个 feature 才算完成 prove。
"""


def _render_features(spec: SkillSpec) -> dict[str, str]:
    files: dict[str, str] = {}
    index = [
        "# Feature map",
        "",
        f"`{spec.app_name}` 的用户可见功能索引。每个文件用四个固定 H2 描述。",
        "Never weaken the journey to make it pass —— 前置数据缺失就报失败并写进 Gotchas。",
        "",
        "| Feature | File |",
        "|---|---|",
    ]
    for name in spec.features:
        body = FEATURE_BODIES[name]
        rel = f"features/{name}.md"
        index.append(f"| {name} | [{name}.md]({name}.md) |")
        files[rel] = "\n".join([
            f"# {name}", "",
            f"## {FEATURE_MAP_H2[0]}", "", body[0], "",
            f"## {FEATURE_MAP_H2[1]}", "", body[1], "",
            f"## {FEATURE_MAP_H2[2]}", "", body[2], "",
            f"## {FEATURE_MAP_H2[3]}", "", body[3], "",
        ])
    files["features/README.md"] = "\n".join(index + [""])
    return files


def generate_skill(
    target: Path,
    *,
    spec: SkillSpec | None = None,
    weaken_drive: bool = False,
    cleanup_eats_evidence: bool = False,
) -> GeneratedSkill:
    """写出整套 skill，返回可绑定到 verdict 的 skill_hash。"""
    spec = spec or build_shop_spec()
    root = (target / f".cursor/skills/verify-{spec.app_name}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "helpers").mkdir(parents=True, exist_ok=True)
    (root / "features").mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {"SKILL.md": _render_skill_md(spec)}
    files.update(_render_features(spec))
    files["helpers/verify_helper.py"] = _render_helper(
        weaken_drive=weaken_drive, eats_evidence=cleanup_eats_evidence
    )
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    helper = root / "helpers" / "verify_helper.py"
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    digest = content_hash({rel: files[rel] for rel in sorted(files)})
    return GeneratedSkill(
        root=root,
        skill_hash=digest,
        files=tuple(sorted(files)),
        spec=spec,
        weakened_drive=weaken_drive,
        cleanup_eats_evidence=cleanup_eats_evidence,
    )
