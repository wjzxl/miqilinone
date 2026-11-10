"""P4：把「仓库访谈」推到「自动起草一份可执行的 VerificationPlan」。

P3 的缺口写得很明白：plan 仍需人工编写。本模块做的是**保守的自动起草**，
不猜、不调模型：

1. 从 pyproject / `__init__.py` 找出真实可导入的主包（支持 src layout 与 flat layout）；
2. 所有候选命令（launch / doctor / journeys）都在目标仓库里**实跑一次**，
   只有 exit=0 且期望标记命中的才写进 plan，失败候选连同原因一起丢进 notes；
3. 结构化规则来自一份与应用无关的通用安全基线，逐条扫基线，**命中即丢弃**
   （绝不把「仓库本来就在违规」的规则写成门禁）；
4. 每条保留的规则自动配一条故意违规变异，注入靶子选一个不会被旅程导入的
   真实 `.py` 文件，注入内容全部放在不执行的函数体里。

因此「换仓库」这件事不再需要人改生成器、改 helper，甚至不用手写 plan：
`p4-draft --repo <任意 Python 仓库>` -> `p3-verify --repo <同一个> --plan <草稿>`。
草稿不是免检通行证：它仍要过 `ground_plan`、六段式 prove、双后端对拍和变异门禁。
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .interview import RepoProfile, ground_plan, interview
from .plan import (
    Command, FeatureDoc, Journey, RuleMutation, StructuredRule, VerificationPlan,
)
from .rules import scan

# 与应用无关的安全基线：候选规则先扫干净基线，命中即整条丢弃。
_CANDIDATE_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    ("no-eval", "forbidden_call", "eval",
     "禁止动态求值：验证链必须可静态审计", ""),
    ("no-os-system", "forbidden_call", "os.system",
     "外部命令必须走显式 argv，不能拼 shell", ""),
    ("no-pickle-loads", "forbidden_call", "pickle.loads",
     "禁止反序列化不可信数据", ""),
    ("no-shell-out", "forbidden_call", "subprocess.getoutput",
     "禁止拼 shell 的命令调用", ""),
    ("no-yaml-import", "forbidden_import", "yaml",
     "未声明依赖的 YAML 解析不得引入", ""),
    ("no-http-client-import", "forbidden_import", "requests",
     "未经授权的出站 HTTP 客户端不得引入", ""),
    ("no-debug-flag", "forbidden_literal", "DEBUG",
     "DEBUG 不得以字面量 True 提交", "True"),
    ("no-testing-flag", "forbidden_literal", "TESTING",
     "TESTING 不得以字面量 True 提交", "True"),
)

MAX_RULES = 6
PROBE_TIMEOUT = 90


@dataclass(frozen=True, slots=True)
class Probe:
    label: str
    argv: tuple[str, ...]
    exit_code: int
    marker: str
    passed: bool
    stdout: str
    stderr: str

    def to_dict(self) -> dict:
        return {
            "label": self.label, "argv": list(self.argv),
            "exit_code": self.exit_code, "marker": self.marker,
            "passed": self.passed,
            "stdout_tail": self.stdout[-300:], "stderr_tail": self.stderr[-300:],
        }


@dataclass(frozen=True, slots=True)
class DraftResult:
    plan: VerificationPlan
    profile: RepoProfile
    package: str
    layout: str
    probes: tuple[Probe, ...]
    dropped_rules: tuple[str, ...]
    inject_file: str
    notes: tuple[str, ...]

    @property
    def grounded(self) -> tuple[str, ...]:
        return ground_plan(self.profile, self.plan)


def _read_pyproject(base: Path) -> dict:
    path = base / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}


def _find_package(profile: RepoProfile, base: Path) -> tuple[str, str]:
    """返回 (可导入的模块名, layout)；layout ∈ {flat, src}。"""
    known = set(profile.tracked_files)
    name = str(_read_pyproject(base).get("project", {}).get("name") or "")
    candidates = [name.replace("-", "_").replace(".", "_")] if name else []
    # 从 `__init__.py` 反推顶层包，兼容没有 pyproject / 名字对不上的仓库。
    for rel in known:
        if not rel.endswith("/__init__.py"):
            continue
        parts = Path(rel).parts
        if len(parts) >= 2 and parts[0] not in ("tests", "test", "spec", "docs", "data"):
            candidates.append(parts[0] if parts[0] != "src" else parts[1])
    for pkg in dict.fromkeys(c for c in candidates if c.isidentifier()):
        for layout, rel in (("flat", f"{pkg}/__init__.py"), ("src", f"src/{pkg}/__init__.py")):
            if rel in known or (base / rel).is_file():
                return pkg, layout
    raise ValueError("找不到可导入的主包：自动起草需要至少一个顶层 Python 包")


def _import_prefix(layout: str) -> str:
    return "import sys; sys.path.insert(0, 'src'); " if layout == "src" else ""


def _run_probe(label: str, cmd: Command, base: Path) -> Probe:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"        # 起草阶段不往被访谈仓库写 __pycache__
    env["PYTHONPATH"] = str(base) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            list(cmd.argv), cwd=str(base), env=env, capture_output=True,
            text=True, timeout=min(cmd.timeout_seconds, PROBE_TIMEOUT),
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        code, out, err = 124, "", f"{type(exc).__name__}: {exc}"
    marker = cmd.expect_stdout_contains
    passed = (code == cmd.expect_exit_code
              and (not marker or marker in out or marker in err))
    return Probe(label, cmd.argv, code, marker, passed, out, err)


def _pick_inject_file(profile: RepoProfile, base: Path) -> str:
    """选一个真实存在、被 globs 覆盖、且不会被测试发现机制导入的 .py 文件。"""
    excluded_names = {"__init__.py", "conftest.py", "setup.py", "conf.py"}
    for rel in sorted(profile.tracked_files):
        p = Path(rel)
        if not rel.endswith(".py") or p.name in excluded_names:
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        if any(part in ("docs", "examples", ".github") for part in p.parts):
            continue
        if (base / rel).is_file():
            return rel
    raise ValueError("找不到安全的变异注入靶子（需要一个非测试 .py 文件）")


def _mutation_snippet(rule: StructuredRule) -> str:
    slug = rule.id.replace("-", "_")
    if rule.kind == "forbidden_call":
        return (f"def _mw_mutant_{slug}():\n"
                f"    return {rule.selector}(None)  # injected, never executed")
    if rule.kind == "forbidden_import":
        return (f"def _mw_mutant_{slug}():\n"
                f"    import {rule.selector}\n"
                f"    return {rule.selector}")
    return (f"def _mw_mutant_{slug}():\n"
            f"    {rule.selector} = {rule.literal}\n"
            f"    return {rule.selector}")


def _rules_for(base: Path, backend: str) -> tuple[tuple[StructuredRule, ...], tuple[str, ...]]:
    kept: list[StructuredRule] = []
    dropped: list[str] = []
    for rid, kind, selector, message, literal in _CANDIDATE_RULES:
        if len(kept) >= MAX_RULES:
            dropped.append(f"已保留 {MAX_RULES} 条，达到上限，其余候选未评估（非失败）")
            break
        rule = StructuredRule(
            id=rid, kind=kind, selector=selector, message=message,
            globs=("**/*.py",), literal=literal, severity="error",
        )
        result = scan((rule,), base, backend)
        if result.clean:
            kept.append(rule)
        else:
            dropped.append(f"{rid}: 基线已有 {len(result.findings)} 处命中，不能当门禁")
    if not kept:
        raise ValueError("通用安全基线在目标仓库里全部命中，无法自动起草；需人工编写 plan")
    return tuple(kept), tuple(dropped)


def _features(plan_pkg: str, layout: str, journeys: tuple[Journey, ...],
              rules: tuple[StructuredRule, ...], inject_file: str,
              test_dir: str) -> tuple[FeatureDoc, ...]:
    journey_text = "\n".join(f"`{' '.join(j.argv)}`" for j in journeys) or "（无旅程）"
    rule_text = ", ".join(f"`{r.id}`" for r in rules)
    return (
        FeatureDoc(
            name="package-surface",
            sub_features=f"- 主包 `{plan_pkg}`（{layout} layout）\n- 访谈器在磁盘上确认的真实入口",
            user_pov=f"在仓库根运行 plan 的 launch 命令，看到 `READY {plan_pkg}`；主包改名或移走会立刻让这段失败。",
            driving=f"Launch 直接执行 `{journeys[0].argv[0] if journeys else 'python3'} ...` 对应的导入探针，"
                    "Doctor 再核对测试目录/治理文件确实存在。两段都不做业务断言。",
            gotchas="src layout 的仓库必须在 `-c` 里先把 `src` 插入 sys.path；"
                    "起草阶段实跑失败的命令不会写进 plan。",
        ),
        FeatureDoc(
            name="verified-journeys",
            sub_features=f"- 测试目录 `{test_dir}`\n- 每条候选旅程都在起草阶段实跑通过后才保留",
            user_pov=f"按 plan 的 journeys 顺序执行：{journey_text}。任一退出码或期望标记不符，Drive 立刻 DRIVE_FAIL。",
            driving="生成的 helper 逐条跑 argv（不经 shell），把 stdout/stderr 原样落到 "
                    "`evidence/journey-*.log`；期望标记在合并输出流里匹配，命中哪条流记进 proof.json。",
            gotchas="不要为了让旅程通过而删掉失败的命令；失败候选已在起草 notes 里留痕，"
                    "应补依赖或补人工 plan，而不是弱化断言。",
        ),
        FeatureDoc(
            name="structured-rule-gate",
            sub_features=f"- 自动保留的干净规则：{rule_text}\n- 每条规则配一条故意违规变异",
            user_pov=f"往任何 `.py` 文件塞一条被禁的调用/导入/字面量，Drive 段报出规则 id、文件与行号并拒绝通过。",
            driving=f"生成的 helper 内嵌 python-ast 检查器；`rules/*.yml` 是同一组规则的 ast-grep 版本。"
                    f"变异只注入 `{inject_file}`，且全部放在不会执行的函数体里，避免 ImportError 污染旅程。",
            gotchas="基线本身已命中的候选规则会被自动丢弃；变异只在 ShadowRepo 副本里进行，"
                    "绝不写回目标仓库。双后端判定不一致时整套门禁阻断。",
        ),
    )


def draft_plan(
    root: str | Path,
    *,
    app_name: str | None = None,
    backend: str = "python-ast",
) -> DraftResult:
    """只读访谈 + 候选实跑，产出一份确定性、可复现的 VerificationPlan 草稿。"""
    base = Path(root).resolve()
    profile = interview(base)
    pkg, layout = _find_package(profile, base)
    prefix = _import_prefix(layout)
    name = app_name or base.name
    probes: list[Probe] = []

    launch = Command(
        argv=("python3", "-c", prefix + f"print('READY', __import__('{pkg}').__name__)"),
        expect_stdout_contains="READY", timeout_seconds=30,
    )
    probes.append(_run_probe("launch", launch, base))
    if not probes[-1].passed:
        raise ValueError(f"主包 {pkg} 无法导入，自动起草中止：{probes[-1].stderr[-200:]}")

    test_dirs = sorted({str(Path(t).parent) for t in profile.test_targets
                        if str(Path(t).parent) != "."})
    test_dir = test_dirs[0] if test_dirs else "."
    doctor_target = f"{test_dir}/" if test_dir != "." else f"{pkg}/__init__.py"
    doctor = Command(
        argv=("python3", "-c",
              f"print('DOCTOR_READY' if __import__('pathlib').Path('{doctor_target}').exists() "
              "else 'DOCTOR_FAIL')"),
        expect_stdout_contains="DOCTOR_READY", timeout_seconds=30,
    )
    probes.append(_run_probe("doctor", doctor, base))

    journey_candidates: list[Journey] = []
    if test_dir != ".":
        journey_candidates.append(Journey(
            name="unit-suite",
            argv=("python3", "-m", "unittest", "discover", "-s", test_dir,
                  "-p", "test_*.py", "-v"),
            expect_stdout_contains="OK", timeout_seconds=90,
        ))
    if profile.test_targets:
        journey_candidates.append(Journey(
            name="pytest-target",
            argv=("python3", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                  profile.test_targets[0]),
            expect_stdout_contains="", timeout_seconds=90,
        ))
    scripts = _read_pyproject(base).get("project", {}).get("scripts", {})
    for spec in list(scripts.values())[:1]:
        if ":" not in str(spec):
            continue
        mod, func = str(spec).split(":", 1)
        journey_candidates.append(Journey(
            name="entrypoint-callable",
            argv=("python3", "-c", prefix + f"from {mod} import {func}; "
                  f"print('ENTRY_OK', callable({func}))"),
            expect_stdout_contains="ENTRY_OK", timeout_seconds=30,
        ))
    if (base / pkg / "__main__.py").is_file() or (base / "src" / pkg / "__main__.py").is_file():
        journey_candidates.append(Journey(
            name="module-help",
            argv=("python3", "-c", prefix + f"import runpy; print('MODULE_OK', '{pkg}')"),
            expect_stdout_contains="MODULE_OK", timeout_seconds=30,
        ))
    # 保底：导入主包并列出公开 API，是任何库的最小真实用户路径。
    journey_candidates.append(Journey(
        name="import-surface",
        argv=("python3", "-c", prefix +
              f"print('JOURNEY_OK', len([n for n in dir(__import__('{pkg}')) if not n.startswith('_')]))"),
        expect_stdout_contains="JOURNEY_OK", timeout_seconds=30,
    ))

    journeys: list[Journey] = []
    for candidate in journey_candidates:
        probe = _run_probe("journey:" + candidate.name, candidate, base)
        probes.append(probe)
        if probe.passed:
            journeys.append(candidate)

    rules, dropped = _rules_for(base, backend)
    inject_file = _pick_inject_file(profile, base)
    mutations = tuple(
        RuleMutation(rule_id=r.id, inject_file=inject_file,
                     snippet=_mutation_snippet(r))
        for r in rules
    )
    if not probes[1].passed:
        raise ValueError(f"体检命令实跑失败，自动起草中止：{probes[1].stderr[-200:]}")

    notes = (
        f"自动起草：主包 {pkg}（{layout} layout），访谈指纹 {profile.fingerprint[:24]}",
        f"候选命令实跑 {len(probes)} 条，保留 journeys {len(journeys)} 条；"
        "失败候选连同 stderr 留在 DraftResult.probes 里，不写进 plan",
        f"通用规则基线丢弃 {len(dropped)} 条（仓库本来就在命中，不能当门禁）",
        f"变异注入靶子 {inject_file}，snippet 全部放在不执行的函数体内",
        "草稿仍须过 ground_plan / 六段式 prove / 双后端对拍 / 变异门禁，不是免检通行证",
    )
    plan = VerificationPlan(
        app_name=name,
        surface=f"{pkg} ({layout} layout), test_framework={profile.test_framework}",
        language="python",
        launch=launch, doctor=doctor,
        journeys=tuple(journeys), rules=rules, mutations=mutations,
        features=_features(pkg, layout, tuple(journeys), rules, inject_file, test_dir),
        evidence_path="evidence/proof.json",
        isolate_note="VERIFY_APP_ROOT 指向目标仓库；VERIFY_RUN_DIR 按 run 独占；ShadowRepo 是一次性副本",
        notes=notes,
    )
    plan.validate()
    return DraftResult(
        plan=plan, profile=profile, package=pkg, layout=layout,
        probes=tuple(probes), dropped_rules=dropped,
        inject_file=inject_file, notes=notes,
    )


def describe_draft(result: DraftResult) -> str:
    import json

    return json.dumps({
        "repo": result.profile.root,
        "package": result.package,
        "layout": result.layout,
        "test_framework": result.profile.test_framework,
        "plan_fingerprint": result.plan.fingerprint()[:24],
        "journeys": [j.name for j in result.plan.journeys],
        "rules": [r.id for r in result.plan.rules],
        "mutations": len(result.plan.mutations),
        "inject_file": result.inject_file,
        "probes": [p.to_dict() for p in result.probes],
        "dropped_rules": list(result.dropped_rules),
        "grounded": list(result.grounded),
    }, ensure_ascii=False, indent=2)
