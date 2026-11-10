"""P3 通用六段式生成器：输入 VerificationPlan + RepoProfile，输出自包含的可执行 skill。

与 P2 generator.py 的本质差别：生成物里**不含任何目标应用的知识**。
helper 读同目录的 plan.json，按其中的 argv 驱动真实命令、按其中的 rules 做
结构化扫描。换仓库只需换一份 plan，生成器与 helper 一行都不改。

产物：
    .cursor/skills/verify-<app>/
      SKILL.md                  六段，命令全部来自访谈 + plan，无占位符
      plan.json                 编译后的契约（helper 的唯一输入）
      rules/<id>.yml            ast-grep 规则文件，可供 CI / 人工直接复用
      helpers/verify_helper.py  0755，内嵌 python-ast 检查器，零 pip 依赖
      features/README.md + ...  四个固定 H2 的 feature map

整体算 skill_hash，绑进 verdict：改计划、改规则、改生成物都要重裁。
"""

from __future__ import annotations

import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from .interview import RepoProfile
from .plan import VerificationPlan, dump_plan
from .rules import rule_to_yaml

FEATURE_H2: tuple[str, ...] = (
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with the verified commands",
    "Gotchas",
)

SIX_PHASES = ("launch", "doctor", "drive", "evidence", "cleanup", "helpers")

# 生成物内嵌的结构化检查器。语义与 rules.scan_python_ast 一致，
# tests/test_p3_rules.py 会把两者对拍锁死，防止「门禁和主流程用的不是一套逻辑」。
PY_AST_CHECKER = r'''
def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return parent + "." + node.attr if parent else node.attr
    return ""


def structured_findings(rules):
    """纯标准库 AST 检查；返回 [{rule_id, file, line, text}]。

    一次解析、多规则共用：N 条规则扫 M 个文件只做 M 次 ast.parse，
    而不是 N*M 次（实测把一次 Drive 从 ~11s 降到 ~2s）。
    """
    parsed = {}
    for rule in rules:
        for rel in _collect(rule.get("globs", ["**/*.py"])):
            if rel in parsed:
                continue
            try:
                source = (APP_ROOT / rel).read_text(encoding="utf-8")
                parsed[rel] = (source, ast.parse(source, filename=rel))
            except (OSError, UnicodeError, SyntaxError):
                parsed[rel] = None
    out = []
    for rule in rules:
        for rel in _collect(rule.get("globs", ["**/*.py"])):
            entry = parsed.get(rel)
            if not entry:
                continue
            source, tree = entry
            for node in ast.walk(tree):
                hit = False
                if rule["kind"] == "forbidden_call" and isinstance(node, ast.Call):
                    hit = _dotted(node.func) == rule["selector"]
                elif rule["kind"] == "forbidden_import":
                    if isinstance(node, ast.Import):
                        hit = any(a.name == rule["selector"]
                                  or a.name.startswith(rule["selector"] + ".")
                                  for a in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        m = node.module or ""
                        hit = m == rule["selector"] or m.startswith(rule["selector"] + ".")
                elif rule["kind"] == "forbidden_literal" and isinstance(node, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == rule["selector"]
                           for t in node.targets):
                        seg = (ast.get_source_segment(source, node.value) or "").strip()
                        hit = seg == rule["literal"]
                if hit:
                    # 只有命中才切源码片段：ast.get_source_segment 按行列回切，
                    # 放在每个节点上会让一次 Drive 平白多花好几秒。
                    text = ast.get_source_segment(source, node) or ""
                    out.append({
                        "rule_id": rule["id"], "file": rel,
                        "line": getattr(node, "lineno", 0),
                        "text": text.replace("\n", " ")[:200],
                    })
    return sorted(out, key=lambda x: (x["rule_id"], x["file"], x["line"]))
'''

HELPER_TEMPLATE = r'''#!/usr/bin/env python3
"""通用六段式执行体，由 P3 生成器产出，可被冷启动 agent 直接执行。

零 pip 依赖、零目标应用知识：所有命令、旅程与结构化规则都来自同目录的 plan.json。
环境变量：VERIFY_APP_ROOT（被验证的仓库）、VERIFY_RUN_DIR（本次 run 的隔离目录）。
expect_stdout_contains 在 stdout+stderr 合并流里匹配（unittest/pytest 的摘要写 stderr），
命中哪条流会原样记进 evidence/proof.json，不靠猜。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
PLAN = json.loads((SKILL_ROOT / "plan.json").read_text(encoding="utf-8"))
APP_ROOT = Path(os.environ["VERIFY_APP_ROOT"]).resolve()
RUN_DIR = Path(os.environ["VERIFY_RUN_DIR"]).resolve()
INSTANCE = RUN_DIR / "instance"
EVIDENCE = RUN_DIR / "evidence"
SKIP_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules", "data", ".workbuddy"}

__PY_AST_CHECKER__


def _collect(globs):
    found = set()
    for pattern in globs:
        for p in APP_ROOT.glob(pattern):
            if not p.is_file():
                continue
            rel = p.relative_to(APP_ROOT)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            found.add(str(rel))
    return sorted(found)


def run_cmd(cmd, label):
    """argv 直传 subprocess，不用 shell；环境带本 run 独占的 VERIFY_DATA_DIR。"""
    env = dict(os.environ)
    env["VERIFY_DATA_DIR"] = str(INSTANCE / "data")
    env["PYTHONPATH"] = str(APP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 不写 .pyc：.pyc 记的是秒级 mtime + size，同长度改动会被当成「缓存仍有效」，
    # 于是「故意改坏的版本」根本没被加载，变异门禁静默失效。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        list(cmd["argv"]), cwd=str(APP_ROOT), env=env,
        capture_output=True, text=True,
        timeout=int(cmd.get("timeout_seconds", 60)),
    )
    marker = cmd.get("expect_stdout_contains", "")
    # 真实 CLI 的摘要未必写 stdout：unittest/pytest 把结果写到 stderr，
    # 只查 stdout 会让一条 exit=0 的用户旅程被误判成失败（P4 外部仓库实测）。
    # 字段名沿用 plan 契约，语义明确为「合并输出流里的标记」，证据里记下命中哪条流。
    in_stdout = bool(marker) and marker in proc.stdout
    in_stderr = bool(marker) and marker in proc.stderr
    ok = (proc.returncode == int(cmd.get("expect_exit_code", 0))
          and (not marker or in_stdout or in_stderr))
    if not marker:
        stream = "n/a"
    elif in_stdout:
        stream = "stdout"
    elif in_stderr:
        stream = "stderr"
    else:
        stream = "MISSING"
    return {
        "label": label, "argv": list(cmd["argv"]),
        "exit_code": proc.returncode, "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-2000:], "expected_marker": marker, "ok": ok,
        "marker_stream": stream,
    }


def phase_launch() -> int:
    # Launch：准备本 run 独占实例 + 跑一次启动探针。
    # 只证明「通路可用」，绝不代入业务断言 —— 那是 Drive 的职责（P2 的教训）。
    (INSTANCE / "data").mkdir(parents=True, exist_ok=True)
    (INSTANCE / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")
    r = run_cmd(PLAN["launch"], "launch")
    if not r["ok"]:
        print("LAUNCH_FAIL " + r["stderr"].strip() + " " + r["stdout"].strip()[-200:])
        return 1
    print("LAUNCH_OK ready=usable instance=" + str(INSTANCE / "data"))
    return 0


def phase_doctor() -> int:
    # Doctor：一次只读体检，回答 "is this instance worth driving?"
    marker = INSTANCE / "owner.pid"
    if not marker.exists() or not (INSTANCE / "data").is_dir():
        print("DOCTOR_FAIL instance not launched by this run")
        return 1
    r = run_cmd(PLAN["doctor"], "doctor")
    if not r["ok"]:
        print("DOCTOR_FAIL unhealthy: " + r["stderr"].strip() + " " + r["stdout"].strip()[-200:])
        return 1
    print("DOCTOR_OK read_only=true isolated_data=true")
    return 0


def phase_drive() -> int:
    # Drive：真实用户旅程 + 结构化代码规则，动作与结果状态成对写进证据。
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    journeys = []
    for spec in PLAN["journeys"]:
        r = run_cmd(spec, spec["name"])
        (EVIDENCE / ("journey-" + spec["name"] + ".log")).write_text(
            r["stdout"] + "\n--- stderr ---\n" + r["stderr"], encoding="utf-8"
        )
        journeys.append(r)
        if not r["ok"]:
            print("DRIVE_FAIL journey " + spec["name"] + ": exit="
                  + str(r["exit_code"]) + " " + r["stderr"].strip()[-200:])
            return 1
    findings = structured_findings(PLAN["rules"])
    if findings:
        print("DRIVE_FAIL structured rules fired: "
              + ", ".join(f["rule_id"] + "@" + f["file"] + ":" + str(f["line"])
                          for f in findings[:8]))
        return 1
    proof = {
        "app": PLAN["app_name"],
        "action": [j["argv"] for j in journeys],
        "command_exit_codes": {j["label"]: j["exit_code"] for j in journeys},
        "marker_streams": {j["label"]: j["marker_stream"] for j in journeys},
        "resulting_state": {j["label"]: (j["stdout"] + j["stderr"]).strip()[-500:]
                            for j in journeys},
        "side_effect": {
            "path": "evidence/journey-*.log",
            "files": sorted(p.name for p in EVIDENCE.glob("journey-*.log")),
            "count": len(list(EVIDENCE.glob("journey-*.log"))),
        },
        "structured_rules": {"applied": [r["id"] for r in PLAN["rules"]],
                             "findings": findings},
        "backend": "python-ast (embedded, zero dependency)",
    }
    (EVIDENCE / "proof.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("DRIVE_OK journeys=" + str(len(journeys)) + " rules="
          + str(len(PLAN["rules"])) + " findings=0 evidence=evidence/proof.json")
    return 0


def phase_evidence() -> int:
    p = EVIDENCE / "proof.json"
    if not p.exists():
        print("EVIDENCE_FAIL proof.json missing")
        return 1
    d = json.loads(p.read_text(encoding="utf-8"))
    required = ("action", "command_exit_codes", "resulting_state",
                "side_effect", "structured_rules")
    missing = [k for k in required if k not in d]
    if missing:
        print("EVIDENCE_FAIL incomplete proof: " + ",".join(missing))
        return 1
    if not d["side_effect"]["files"]:
        print("EVIDENCE_FAIL no journey log survived")
        return 1
    if d["structured_rules"]["findings"]:
        print("EVIDENCE_FAIL rule findings recorded in a passing proof")
        return 1
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    print("EVIDENCE_OK action+state+side_effect captured sha256=" + digest[:16])
    return 0


def phase_cleanup() -> int:
    # Cleanup：只删本 run 创建的 instance；绝不删 evidence，绝不按进程名 kill。
    shutil.rmtree(INSTANCE, ignore_errors=True)
    if os.environ.get("VERIFY_EATS_EVIDENCE"):
        shutil.rmtree(EVIDENCE, ignore_errors=True)
    print("CLEANUP_OK instance_removed=true evidence_preserved=true")
    return 0


def phase_helpers() -> int:
    helper = Path(__file__)
    if not os.access(str(helper), os.X_OK):
        print("HELPERS_FAIL helper is not executable")
        return 1
    body = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    missing = [p for p in ("launch", "doctor", "drive", "evidence", "cleanup", "helpers")
               if ("verify_helper.py " + p) not in body]
    if missing:
        print("HELPERS_FAIL invocation not documented for: " + ",".join(missing))
        return 1
    if not (SKILL_ROOT / "plan.json").is_file():
        print("HELPERS_FAIL plan.json missing")
        return 1
    print("HELPERS_OK executable=true invocations_documented=6 plan_embedded=true")
    return 0


PHASES = {
    "launch": phase_launch, "doctor": phase_doctor, "drive": phase_drive,
    "evidence": phase_evidence, "cleanup": phase_cleanup, "helpers": phase_helpers,
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


@dataclass(frozen=True, slots=True)
class GeneratedGenericSkill:
    root: Path
    skill_hash: str
    files: tuple[str, ...]
    plan: VerificationPlan
    profile: RepoProfile

    @property
    def app_name(self) -> str:
        return self.plan.app_name


def _cmdline(cmd) -> str:
    return shlex.join(cmd.argv)


def _render_skill_md(plan: VerificationPlan, profile: RepoProfile) -> str:
    journey_lines = "\n".join("    " + _cmdline(j) for j in plan.journeys)
    feature_lines = "\n".join(f"- `{f.name}`" for f in plan.features)
    rule_lines = "\n".join(
        f"- `{r.id}` ({r.kind}: `{r.selector}`) — {r.message}" for r in plan.rules
    )
    calls = "\n".join(f"    python3 helpers/verify_helper.py {p}" for p in SIX_PHASES)
    notes = "\n".join(f"- {n}" for n in plan.notes) or "- （无补充约束）"
    return f"""---
name: verify-{plan.app_name}
description: "Drive {plan.app_name} ({plan.surface}) like a real user, enforce structured code rules, and capture replayable proof. Use before handing over any change to this repo."
disable-model-invocation: true
---

# Verify {plan.app_name}

本 skill 由**仓库访谈 + 可配置验证计划**编译生成，生成物不含目标应用的硬编码知识。
所有命令、旅程与规则来自同目录的 `plan.json`；换仓库只需换一份计划。
访谈指纹：`{profile.fingerprint[:24]}`（仓库内容一变即失效，旧裁决需重裁）。
执行前设置：`VERIFY_APP_ROOT=<被验证的仓库>`、`VERIFY_RUN_DIR=<本次 run 的隔离目录>`。

## Launch

    {_cmdline(plan.launch)}

`python3 helpers/verify_helper.py launch` 创建本 run 独占的 `instance/data` 与
`owner.pid`，再跑上面的启动探针。**Ready means usable, not just that a process
answers**；探针只验证通路，不代入业务断言 —— 期望输出属于 Drive，写进 Launch
会让输出格式类变体在 Drive 之前被拦下，变异测试因此失真。

## Doctor

    {_cmdline(plan.doctor)}
    python3 helpers/verify_helper.py doctor

一次只读体检：instance 由本 run 创建、隔离数据目录存在、上面的体检命令通过。
不写任何文件；任何环节看着不对时，先跑这一段再决定要不要继续 Drive。

## Drive

    {journey_lines}
    python3 helpers/verify_helper.py drive

真实用户路径：逐条执行上面的命令，断言退出码与**合并输出流（stdout + stderr）**里的
期望标记 —— unittest / pytest 的结果摘要走 stderr，只查 stdout 会把成功的旅程误判为失败。
每条旅程的完整 stdout/stderr 落到 `evidence/journey-*.log`，标记命中哪条流记进 proof.json。
随后用内嵌的 **python-ast 结构化检查器**
扫描以下规则（不是字符串 contains）：

{rule_lines}

任何一条命中即 `DRIVE_FAIL`。动作、结果状态、副作用文件清单与规则结果成对写入
`{plan.evidence_path}`。禁止 import 内部函数代替用户路径、禁止 test-only 端点。

## Evidence

    python3 helpers/verify_helper.py evidence

proof.json 必须同时含 action、command_exit_codes、resulting_state、side_effect、
structured_rules；journey 日志必须真实存在，且通过路径里不得残留规则命中。
隔离说明：{plan.isolate_note}。

## Cleanup

    python3 helpers/verify_helper.py cleanup

只删本 run 创建的 `instance/`（含 owner.pid 与 scratch data），**绝不按进程名 kill**，
**绝不删 evidence/**。Cleanup 之后证据必须仍在上面写明的路径；
清理把证据吃掉即判定失败。失败迭代也必须跑 cleanup。

## Helpers

    {calls}

`helpers/verify_helper.py` 由生成器赋予可执行位（0755），六个子命令的调用全部写在
本文件正文里。`rules/*.yml` 是同一组规则的 ast-grep 版本，可直接用于 CI。
`helpers` 子命令自检可执行位、本文档覆盖度与 plan.json 是否随生成物一起交付。

## Features

{feature_lines}

feature map 见 `features/README.md`，每个文件用四个固定 H2 描述。

## Prove before handover

按 launch → doctor → drive → evidence → cleanup → helpers 跑一遍，cleanup 后确认
`{plan.evidence_path}` 仍存在。**从未执行过的 skill 只是 draft，不得用于裁决。**
每条结构化规则都必须经「故意违规 → 规则开火」的变异证明，未全数触发则整套门禁
不得启用。补充约束：

{notes}
"""


def _render_features(plan: VerificationPlan) -> dict[str, str]:
    files: dict[str, str] = {}
    index = [
        "# Feature map", "",
        f"`{plan.app_name}` 的用户可见功能索引。每个文件用四个固定 H2 描述。",
        "Never weaken the journey to make it pass —— 前置数据缺失就报失败并写进 Gotchas。",
        "", "| Feature | File |", "|---|---|",
    ]
    for f in plan.features:
        rel = f"features/{f.name}.md"
        index.append(f"| {f.name} | [{f.name}.md]({f.name}.md) |")
        files[rel] = "\n".join([
            f"# {f.name}", "",
            f"## {FEATURE_H2[0]}", "", f.sub_features, "",
            f"## {FEATURE_H2[1]}", "", f.user_pov, "",
            f"## {FEATURE_H2[2]}", "", f.driving, "",
            f"## {FEATURE_H2[3]}", "", f.gotchas, "",
        ])
    files["features/README.md"] = "\n".join(index + [""])
    return files


def generate_generic_skill(
    target: Path,
    plan: VerificationPlan,
    profile: RepoProfile,
    *,
    cleanup_eats_evidence: bool = False,
) -> GeneratedGenericSkill:
    plan.validate()
    root = (Path(target) / f".cursor/skills/verify-{plan.app_name}").resolve()
    (root / "helpers").mkdir(parents=True, exist_ok=True)
    (root / "features").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)

    helper = HELPER_TEMPLATE.replace("__PY_AST_CHECKER__", PY_AST_CHECKER.strip("\n"))
    leftovers = [t for t in ("__PY_AST_CHECKER__",) if t in helper]
    if leftovers:
        raise ValueError(f"通用 helper 模板里残留未替换的占位符：{leftovers}")
    if cleanup_eats_evidence:
        # 模拟「生成器产出了一份有缺陷的 skill」：cleanup 无条件删证据。
        # §4 的 prove 必须因此判 FAILED，而不是靠执行时传 --eats-evidence 才暴露。
        broken = 'if os.environ.get("VERIFY_EATS_EVIDENCE"):'
        if broken not in helper:
            raise ValueError("helper 模板的 cleanup 分支变了，无法注入缺陷")
        helper = helper.replace(broken, "if True:  # GENERATED DEFECT: cleanup eats evidence", 1)

    files: dict[str, str] = {
        "SKILL.md": _render_skill_md(plan, profile),
        "plan.json": dump_plan(plan),
        "helpers/verify_helper.py": helper,
    }
    files.update(_render_features(plan))
    for rule in plan.rules:
        files[f"rules/{rule.id}.yml"] = rule_to_yaml(rule)

    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    hp = root / "helpers" / "verify_helper.py"
    hp.chmod(hp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    digest = content_hash({rel: files[rel] for rel in sorted(files)})
    return GeneratedGenericSkill(
        root=root, skill_hash=digest, files=tuple(sorted(files)),
        plan=plan, profile=profile,
    )
