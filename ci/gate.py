#!/usr/bin/env python3
"""P4 CI 门禁唯一入口：本地和 GitHub Actions 跑的是同一条命令。

设计原则：**CI 只是薄壳，门禁逻辑住在仓库里**。workflow 不复制一堆 shell 判断，
只负责装依赖并调用 `python3 ci/gate.py`；这样「CI 绿了但本地跑不过」或者反过来
都不会发生，而且门禁自己也能被测试、被回放、被故意破坏验证。

十三项检查（任何一项 FAIL 即 exit 1）：

1. unit_suite          默认 unittest discover（不依赖 Postgres / DBOS）
2. self_plan           主体工程自举：六段式 + 双后端 + 变异门禁 + 六锚点
3. external_plan       异构外部仓库（flat layout / spec/ 目录 / 另一套规则）
4. external_astgrep    同一外部仓库改用 ast-grep 作主后端
5. autodraft           连 plan 都不手写，访谈自动起草后直接端到端验证
6. no_app_knowledge    外部仓库生成物里不得出现主体工程的硬编码知识
7. rules_in_ci         生成的 rules/*.yml 交给真正的 ast-grep CLI 扫：基线干净、
                        每条规则注入违规后必须命中（不经过我们自己的引擎）
8. pr_rules_scan        PR 改动的 .py（非 PR 环境则全量）逐条过同样的 YAML 规则
9. negative_gate       弱化规则 / 吃掉证据必须被阻断
10. head_sha_sensitivity 同内容重跑一致；改 plan / 改仓库必须重裁
11. pr_diff_gate   [P5] 真实 git commit / 分支的三方 diff：干净 PR 绿、违规 PR
                        精确判红、重命名删除不崩、历史遗留违规不误伤本 PR
12. runtime_isolation [P5] env 白名单 / rlimits 实测 / 只读源快照 / 超时 SIGKILL /
                        容器接缝，并如实标注本机没强制的 fs 与网络边界
13. telemetry_budget [P5] span 哈希链可被证伪、GenAI semconv pin、预算四维熔断
                        黏性 kill switch、账本重放一致

产出 JSON 报告（默认 data/ci/gate-report.json），每项带耗时与证据，便于回顾
「这次到底是哪项门禁红、为什么红」。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CI_DIR = ROOT / "ci"
for entry in (SRC, CI_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from michelin.chief.p3_workflow import P3Pipeline, P3Scenario  # noqa: E402
from michelin.chief.policy import TaskIntent  # noqa: E402
from michelin.context.sources import SourcePlane  # noqa: E402
from michelin.delivery.replay import replay  # noqa: E402
from michelin.models import ScriptedModel  # noqa: E402
from michelin.platform.envelope import SEMCONV_VERSION, content_hash  # noqa: E402
from michelin.platform.store import EventStore  # noqa: E402
from michelin.verification.autodraft import draft_plan  # noqa: E402
from michelin.verification.generic_mutants import ShadowRepo  # noqa: E402
from michelin.verification.interview import ground_plan  # noqa: E402
from michelin.verification.plan import load_plan  # noqa: E402
from michelin.verification.rules import collect_files, find_ast_grep  # noqa: E402
from michelin.runtime import (  # noqa: E402
    Budget, BudgetExceeded, BudgetGuard, Charge, RunHarness, SandboxPolicy, SpanLog,
    rlimit_disclosure,
)
from pr_diff import export_tree, read_pr_diff  # noqa: E402
from pr_fixture import ensure_demo_repo  # noqa: E402

FIXTURE = ROOT / "fixtures" / "ledger-kit"
HAND_PLAN = ROOT / "plans" / "ledger-kit.json"
SELF_PLAN = ROOT / "plans" / "michelin-self.json"

# 生成物里一旦出现这些字符串，就说明生成器偷偷塞回了目标应用知识。
HOST_ONLY_MARKERS = (
    "src/michelin", "tests/test_p3_rules.py", "michelin-self.json",
    "shop.cli", "TOTAL=31.80", "michelin-workbench",
)

INTENT = TaskIntent(
    intent="P4 CI 门禁：验证通用验证装置在任意仓库上可复现、会开火、会阻断",
    source_bodies=(
        "【一手原文】A generated skill that was never executed is a draft, not a deliverable.",
        "【治理原文】每条规则都要经故意违规证明会开火；门禁必须能变红，而不只是常绿。",
    ),
    source_system="ci-gate-p4",
)


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def scan_generated_rules(exe: str, rules_dir: Path, root: Path,
                         files: tuple[str, ...]) -> dict:
    """把生成的 YAML 规则直接交给 ast-grep CLI —— CI 里真正的执行体。

    这里刻意不调用 michelin.verification.rules：CI 不信任「自己写的检查器」
    自证，规则文件必须是能被独立工具消费的资产。
    """
    scanned: list[dict] = []
    findings: list[dict] = []
    # cwd 可能是深层导出目录 / ShadowRepo 副本；相对的可执行文件或规则路径在那里会失效。
    exe_path = str(Path(exe).resolve())
    for yml in sorted(Path(rules_dir).resolve().glob("*.yml")):
        if not files:
            continue
        proc = subprocess.run(
            [exe_path, "scan", "--rule", str(yml.resolve()), "--json", *files],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"ast-grep 执行 {yml.name} 失败 (exit={proc.returncode}): "
                f"{proc.stderr.strip()[:300]}"
            )
        hits = json.loads(proc.stdout or "[]")
        scanned.append({"rule": yml.name, "exit_code": proc.returncode,
                        "hits": len(hits)})
        for hit in hits:
            findings.append({
                "rule": yml.stem, "file": hit.get("file", ""),
                "line": hit.get("range", {}).get("start", {}).get("line", 0) + 1,
                "text": str(hit.get("text", ""))[:120],
            })
    return {"clean": not findings, "scanned": scanned,
            "findings": findings, "files_scanned": len(files)}


def _pr_files(root: Path, globs: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    """P5：真实 PR 走三方 diff；不是 git 仓库 / 找不到 base 才回退全量树。

    `pr_diff.py` 统一处理 GITHUB_BASE_REF、MW_PR_BASE、重命名/删除与 NUL 文件名。
    0 个 .py 变更也是合法的 PR（只改文档），此时返回空列表而不是误回退全量。
    """
    diff = read_pr_diff(root)
    if diff is not None:
        return diff.python_files, diff.source
    return tuple(collect_files(root, globs)), "full-tree"


class Gate:
    def __init__(self, *, fast: bool = False, require_ast_grep: bool = True):
        self.fast = fast
        self.require_ast_grep = require_ast_grep
        self.tmp = ROOT / "data" / "ci" / "runs"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.checks: list[dict] = []
        self.cache: dict[str, dict] = {}
        self.exe = find_ast_grep()

    def _check(self, name: str, func) -> None:
        started = time.monotonic()
        try:
            ok, evidence = func()
        except Exception as exc:  # 门禁本身出错也是 FAIL，不能静默跳过
            ok, evidence = False, {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-800:],
            }
        skipped = bool(isinstance(evidence, dict) and evidence.get("skipped"))
        entry = {
            "name": name, "ok": bool(ok), "skipped": skipped,
            "seconds": round(time.monotonic() - started, 2),
            "evidence": _jsonable(evidence),
        }
        self.checks.append(entry)
        flag = "SKIP" if skipped else ("PASS" if ok else "FAIL")
        detail = evidence.get("summary", "") if isinstance(evidence, dict) else ""
        print(f"[{flag}] {name:<22} {entry['seconds']:>6.2f}s  {detail}", flush=True)
        if not ok and isinstance(evidence, dict):
            for key in ("error", "reason", "state", "blocked_reason"):
                if evidence.get(key):
                    print(f"       ! {key}: {str(evidence[key])[:300]}", flush=True)

    # ---- 共用：跑一次完整 P3 流水线，返回可序列化的证据 ----
    def _pipeline(self, key: str, repo: Path, plan, *, backend: str = "python-ast",
                   scenario: P3Scenario | None = None) -> dict:
        store = EventStore(":memory:")
        try:
            pipeline = P3Pipeline(
                store, SourcePlane(store),
                ScriptedModel("openai", "gpt-p4-gate", ["PASS"]),
                repo, plan, self.tmp, backend=backend, ast_grep=self.exe,
            )
            result = pipeline.handle(INTENT, scenario or P3Scenario())
            view = replay(store, result.run_id)
            chain_ok, count, bad = store.verify_chain()
            types = [t["type"] for t in view.timeline]
            payload = {
                "run_id": result.run_id,
                "state": result.state.value,
                "verdict": result.verdict,
                "skill_status": result.skill_status,
                "skill_hash": result.skill_hash[:24],
                "head_sha": result.head_sha,
                "repo_fingerprint": result.repo_fingerprint[:24],
                "plan_fingerprint": result.plan_fingerprint[:24],
                "backend": result.backend,
                "cross_checked": result.cross_checked,
                "rules_all_fired": result.rules_all_fired,
                "blocked_reason": result.blocked_reason,
                "events": len(view.timeline),
                "six_anchors": view.complete,
                "replay_chain_ok": view.chain_ok,
                "store_chain_ok": chain_ok,
                "store_chain_count": count,
                "store_chain_bad": bad,
                "has_verdict": "verdict.issued" in types,
                "has_delivery": "artifact.delivered" in types,
                "skill_root": result.skill_root,
                "result": result,
            }
            self.cache[key] = payload
            return payload
        finally:
            store.close()

    def _delivered(self, key: str, func) -> tuple[bool, dict]:
        p = func()
        ok = (p["state"] == "ACCEPTED" and p["verdict"] == "PASS"
              and p["skill_status"] == "PROVEN" and p["rules_all_fired"]
              and p["six_anchors"] and p["replay_chain_ok"] and p["store_chain_ok"]
              and p["has_verdict"] and p["has_delivery"])
        return ok, {"summary": f"{p['state']}/{p['verdict']} events={p['events']} "
                              f"mutations_fired={p['rules_all_fired']}", **{
                                  k: v for k, v in p.items() if k != "result"}}

    # ---- 1. 默认单测 ----
    def check_unit(self):
        """跑默认单测。

        完整输出落盘（data/ci/unit-suite.log，随 artifact 上传）——只留 600 字符
        的尾巴会让 CI 变红时看不到真正的原因，必须能翻到完整现场。
        """
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        log_path = ROOT / "data" / "ci" / "unit-suite.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(combined, encoding="utf-8")
        tail = proc.stderr.strip()[-600:]
        # 失败块比尾巴有用：从 FAIL:/ERROR: 开始截，完整收进证据。
        failures: list[str] = []
        lines = combined.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(("FAIL: ", "ERROR: ")):
                failures.append("\n".join(lines[i:i + 25]))
        return proc.returncode == 0, {
            "summary": tail.splitlines()[-1] if tail else "no output",
            "exit_code": proc.returncode, "stderr_tail": tail,
            "failure_blocks": failures[:4],
            "full_log": str(log_path.relative_to(ROOT)),
        }

    # ---- 2~5. 四条端到端流水线 ----
    def check_self(self):
        return self._delivered("self", lambda: self._pipeline(
            "self", ROOT, load_plan(SELF_PLAN)))

    def check_external(self):
        return self._delivered("external", lambda: self._pipeline(
            "external", FIXTURE, load_plan(HAND_PLAN)))

    def check_external_astgrep(self):
        if not self.exe:
            return (not self.require_ast_grep), {
                "summary": "ast-grep 不可用", "error": "CI 要求安装 ast-grep-cli",
                "skipped": not self.require_ast_grep,
            }
        return self._delivered("external_asg", lambda: self._pipeline(
            "external_asg", FIXTURE, load_plan(HAND_PLAN), backend="ast-grep"))

    def check_autodraft(self):
        draft = draft_plan(FIXTURE, app_name="ledger-kit-auto")
        problems = ground_plan(draft.profile, draft.plan)
        p = self._pipeline("autodraft", FIXTURE, draft.plan)
        hand = load_plan(HAND_PLAN)
        ok = (not problems and bool(draft.plan.journeys)
              and len(draft.plan.mutations) == len(draft.plan.rules)
              and draft.plan.fingerprint() != hand.fingerprint()
              and p["state"] == "ACCEPTED" and p["verdict"] == "PASS"
              and p["rules_all_fired"])
        return ok, {
            "summary": f"draft={draft.plan.fingerprint()[:16]} "
                       f"journeys={len(draft.plan.journeys)} "
                       f"rules={len(draft.plan.rules)} -> {p['state']}/{p['verdict']}",
            "package": draft.package, "layout": draft.layout,
            "probes": [q.to_dict() for q in draft.probes],
            "dropped_rules": list(draft.dropped_rules),
            "inject_file": draft.inject_file,
            "grounded": list(problems),
            "differs_from_handwritten_plan": draft.plan.fingerprint() != hand.fingerprint(),
            "pipeline": {k: v for k, v in p.items() if k != "result"},
        }

    # ---- 6. 生成物不含主体工程知识 ----
    def check_no_app_knowledge(self):
        hits: list[dict] = []
        inspected = 0
        for key in ("external", "external_asg", "autodraft"):
            p = self.cache.get(key)
            root = p.get("skill_root") if p else None
            if not root or not Path(root).is_dir():
                return False, {"summary": f"缺少生成物 {key}", "error": "没有可检查的 skill_root"}
            for f in sorted(Path(root).rglob("*")):
                if not f.is_file():
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                inspected += 1
                for marker in HOST_ONLY_MARKERS:
                    if marker in text:
                        hits.append({"run": key, "file": str(f.relative_to(root)),
                                     "marker": marker})
        return not hits, {
            "summary": f"检查 {inspected} 个生成文件 / {len(HOST_ONLY_MARKERS)} 个主体知识标记",
            "files_inspected": inspected, "host_only_markers": list(HOST_ONLY_MARKERS),
            "leaks": hits,
        }

    # ---- 7. 生成的 YAML 规则在 CI 里真的会开火 ----
    def check_rules_in_ci(self):
        if not self.exe:
            return (not self.require_ast_grep), {
                "summary": "ast-grep 不可用",
                "error": "CI 要求安装 ast-grep-cli；rules/*.yml 无法独立执行",
                "skipped": not self.require_ast_grep,
            }
        p = self.cache["external"]
        plan = load_plan(HAND_PLAN)
        rules_dir = Path(p["skill_root"]) / "rules"
        files = tuple(collect_files(FIXTURE, ("ledgerkit/**/*.py", "spec/**/*.py")))
        baseline = scan_generated_rules(self.exe, rules_dir, FIXTURE, files)
        cases: list[dict] = []
        with ShadowRepo(FIXTURE) as shadow:
            for m in plan.mutations:
                shadow.inject(m.inject_file, m.snippet)
                result = scan_generated_rules(
                    self.exe, rules_dir, shadow.root,
                    tuple(collect_files(shadow.root, ("ledgerkit/**/*.py", "spec/**/*.py"))),
                )
                matched = [f for f in result["findings"]
                           if f["rule"] == m.rule_id
                           and Path(f["file"]).name == Path(m.inject_file).name]
                cases.append({
                    "rule_id": m.rule_id,
                    "clean_baseline_rule": baseline["clean"],
                    "hits_after_injection": len(result["findings"]),
                    "matched_rule_and_file": bool(matched),
                    "findings": matched[:3],
                })
                shadow.restore()
        ok = baseline["clean"] and bool(cases) and all(
            c["matched_rule_and_file"] for c in cases
        )
        return ok, {
            "summary": f"ast-grep 独立扫描：基线 clean={baseline['clean']}，"
                       f"{sum(c['matched_rule_and_file'] for c in cases)}/{len(cases)} 条变异命中",
            "ast_grep": self.exe,
            "rules_used": [s["rule"] for s in baseline["scanned"]],
            "baseline": baseline, "mutation_cases": cases,
        }

    # ---- 8. PR 改动文件过同一批规则 ----
    def check_pr_rules(self):
        if not self.exe:
            return (not self.require_ast_grep), {
                "summary": "ast-grep 不可用", "skipped": not self.require_ast_grep,
            }
        self_plan = load_plan(SELF_PLAN)
        globs = tuple(dict.fromkeys(g for r in self_plan.rules for g in r.globs))
        files, source = _pr_files(ROOT, globs)
        p = self.cache.get("self")
        rules_dir = Path(p["skill_root"]) / "rules" if p else None
        if not rules_dir or not rules_dir.is_dir():
            return False, {"summary": "缺少自举生成物", "error": "self 流水线未产出 rules/"}
        result = scan_generated_rules(self.exe, rules_dir, ROOT, files)
        # 负向自检：往副本里注入一处 eval，同一套扫描逻辑必须判红。
        with ShadowRepo(ROOT) as shadow:
            target = "tests/test_holdout.py"
            shadow.inject(target, "def _gate_selftest():\n    return eval('1 + 1')")
            poisoned = scan_generated_rules(
                self.exe, rules_dir, shadow.root,
                tuple(collect_files(shadow.root, globs)),
            )
        ok = result["clean"] and not poisoned["clean"]
        return ok, {
            "summary": f"{source}: 扫 {len(files)} 个 .py，基线命中 "
                       f"{len(result['findings'])}，注入违规后命中 "
                       f"{len(poisoned['findings'])}（证明门禁会红）",
            "files_source": source, "files": list(files[:20]),
            "baseline": result, "poisoned": {
                "clean": poisoned["clean"], "findings": poisoned["findings"][:5],
            },
        }

    # ---- 9a. 负向：门禁必须能阻断 ----
    def check_negative(self):
        plan = load_plan(HAND_PLAN)
        blind = self._pipeline("blind", FIXTURE, plan,
                               scenario=P3Scenario(weaken_rules=True))
        eaten = self._pipeline("eaten", FIXTURE, plan,
                               scenario=P3Scenario(cleanup_eats_evidence=True))
        ok = (blind["state"] == "BLOCKED_NEEDS_HUMAN" and not blind["has_verdict"]
              and not blind["has_delivery"] and not blind["rules_all_fired"]
              and eaten["state"] == "BLOCKED_NEEDS_HUMAN" and not eaten["has_verdict"]
              and eaten["skill_status"] == "FAILED")
        return ok, {
            "summary": f"blind_gate={blind['state']}/{blind['rules_all_fired']} "
                       f"evidence_eaten={eaten['state']}/{eaten['skill_status']}",
            "blind_gate": {k: v for k, v in blind.items() if k != "result"},
            "evidence_eaten": {k: v for k, v in eaten.items() if k != "result"},
        }

    # ---- 9b. head_sha：可复现 + 对内容敏感 ----
    def check_head_sha(self):
        if self.fast:
            return True, {"summary": "fast 模式跳过", "skipped": True}
        plan = load_plan(HAND_PLAN)
        a = self._pipeline("head_a", FIXTURE, plan)
        b = self._pipeline("head_b", FIXTURE, plan)
        changed = replace(plan, notes=(*plan.notes, "gate: 改了计划就要重裁"))
        c = self._pipeline("head_c", FIXTURE, changed)
        shadow_dir = self.tmp / "head-content-variant"
        shutil.rmtree(shadow_dir, ignore_errors=True)
        shutil.copytree(FIXTURE, shadow_dir,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        core = shadow_dir / "ledgerkit" / "core.py"
        core.write_text(core.read_text(encoding="utf-8")
                        + "\n# gate: 仓库内容变了，旧裁决必须失效\n", encoding="utf-8")
        d = self._pipeline("head_d", shadow_dir, plan)
        shutil.rmtree(shadow_dir, ignore_errors=True)
        ok = (a["head_sha"] == b["head_sha"]
              and a["head_sha"] != c["head_sha"]
              and a["head_sha"] != d["head_sha"]
              and a["repo_fingerprint"] != d["repo_fingerprint"])
        return ok, {
            "summary": f"同内容重跑一致={a['head_sha'] == b['head_sha']}，"
                       f"改计划={a['head_sha'] != c['head_sha']}，"
                       f"改仓库={a['head_sha'] != d['head_sha']}",
            "same_repo_same_plan": [a["head_sha"], b["head_sha"]],
            "changed_plan": c["head_sha"], "changed_repo": d["head_sha"],
            "repo_fingerprints": [a["repo_fingerprint"], d["repo_fingerprint"]],
            "states": [a["state"], b["state"], c["state"], d["state"]],
        }

    # ---- 11. [P5] 真实 git PR 三方 diff ----
    def check_pr_diff(self):
        if not self.exe:
            return (not self.require_ast_grep), {
                "summary": "ast-grep 不可用", "skipped": not self.require_ast_grep,
            }
        demo = ensure_demo_repo(ROOT / "data" / "ci" / "pr-repos" / "demo")
        p = self.cache.get("self")
        rules_dir = Path(p["skill_root"]) / "rules" if p else None
        if not rules_dir or not rules_dir.is_dir():
            return False, {"summary": "缺少自举生成物", "error": "self 流水线未产出 rules/"}
        exports = self.tmp / "pr-exports"
        cases: dict[str, dict] = {}
        for branch in ("pr-clean", "pr-violation", "pr-rename"):
            diff = read_pr_diff(demo.root, base="main", head=branch)
            out = diff.materialize(exports / branch)
            scan = scan_generated_rules(self.exe, rules_dir, out, diff.python_files)
            cases[branch] = {
                "diff": diff.to_dict(), "scanned_files": list(diff.python_files),
                "clean": scan["clean"], "findings": scan["findings"],
                "rules_scanned": [s["rule"] for s in scan["scanned"]],
            }

        # 对照：全量扫 main 会把历史遗留违规扫出来；增量扫 pr-violation 只该红新文件。
        full_files, full_root = export_tree(demo.root, "main", exports / "main-full")
        full_scan = scan_generated_rules(self.exe, rules_dir, full_root, full_files)

        # 同一个解析器走环境变量通道（GitHub Actions 的 GITHUB_BASE_REF 同源）。
        old = os.environ.get("MW_PR_BASE")
        os.environ["MW_PR_BASE"] = "main"
        try:
            env_diff = read_pr_diff(demo.root, head="pr-violation")
        finally:
            if old is None:
                os.environ.pop("MW_PR_BASE", None)
            else:
                os.environ["MW_PR_BASE"] = old

        clean, viol, ren = cases["pr-clean"], cases["pr-violation"], cases["pr-rename"]
        changed_clean = [c["path"] for c in clean["diff"]["changed"]]
        changed_ren = [c["status"] for c in ren["diff"]["changed"]]
        checks = {
            "clean_pr_green": clean["clean"] and clean["scanned_files"] == ["feature.py"],
            "non_python_change_ignored": "README.md" in changed_clean,
            "violation_pr_red": not viol["clean"],
            "violation_precisely_located": any(
                h["rule"] == "no-eval-in-spine" and h["file"] == "evil.py"
                for h in viol["findings"]),
            "incremental_only": viol["scanned_files"] == ["evil.py"]
            and "legacy.py" not in viol["scanned_files"],
            "full_scan_would_have_failed": any(h["file"] == "legacy.py"
                                               for h in full_scan["findings"]),
            "rename_and_delete_parsed": ren["clean"]
            and ren["scanned_files"] == ["renamed.py"]
            and any(s.startswith("R") for s in changed_ren)
            and any(s.startswith("D") for s in changed_ren),
            "env_base_resolution": env_diff is not None
            and env_diff.python_files == ("evil.py",) and env_diff.base_ref == "main",
        }
        return all(checks.values()), {
            "summary": f"真实 git 三方 diff：{len(cases)} 个 PR 分支，"
                       f"违规命中 {len(viol['findings'])} 处，"
                       f"全量对照命中 legacy.py={any(h['file'] == 'legacy.py' for h in full_scan['findings'])}",
            "repo": demo.to_dict(), "checks": checks, "cases": cases,
            "full_tree_baseline": {
                "files": list(full_files), "clean": full_scan["clean"],
                "findings": full_scan["findings"],
            },
        }

    # ---- 12. [P5] 每任务进程级隔离 ----
    def check_runtime_isolation(self):
        work = self.tmp / "runtime" / f"iso-{time.time_ns()}"
        src = work / "source"
        src.mkdir(parents=True, exist_ok=True)
        (src / "first_context.py").write_text("VALUE = 1\n", encoding="utf-8")
        secret = "MW_P5_GATE_SECRET_DO_NOT_LEAK"
        os.environ[secret] = "leak-value-42"
        py = sys.executable
        try:
            harness = RunHarness(
                work / "run", "p5-isolation",
                policy=SandboxPolicy(timeout_seconds=5.0),
                budget=Budget(max_actions=20),
            )
            env_rec = harness.execute(
                "env", [py, "-S", "-c", "import os,json;print(json.dumps(dict(os.environ)))"])
            write_rec = harness.execute(
                "write-source",
                [py, "-S", "-c",
                 "import os;p=os.environ['MW_SOURCE_ROOT']+'/first_context.py';open(p,'a').write('x')"],
                source_root=src)
            slow_rec = harness.execute(
                "slow", [py, "-S", "-c", "import time;time.sleep(10)"],
                policy=SandboxPolicy(timeout_seconds=0.6))
            report = harness.close()
            container = harness.container_plan([py, "-S", "-c", "pass"],
                                               source_root=src, label="plan")
        finally:
            os.environ.pop(secret, None)

        child_env = json.loads(env_rec.stdout) if env_rec.ok else {}
        disclosure = rlimit_disclosure(env_rec)
        checks = {
            "secret_not_in_child": secret not in child_env
            and "leak-value-42" not in env_rec.stdout,
            "secret_counted_as_dropped": secret in env_rec.sensitive_dropped,
            "home_inside_box": child_env.get("HOME", "").endswith("/box/home"),
            "user_site_disabled": child_env.get("PYTHONNOUSERSITE") == "1",
            "source_write_blocked": write_rec.exit_code != 0
            and "PermissionError" in write_rec.stderr,
            "source_fingerprint_recorded": bool(write_rec.source_fingerprint),
            "timeout_sigkill": slow_rec.timed_out and slow_rec.exit_code == -9
            and slow_rec.duration_ms < 3000,
            # rlimit 的披露必须与实测严格一致，且至少有一项真的生效。
            # **不预设平台一定会忽略某一项**：macOS 忽略 RLIMIT_AS，Linux 不忽略，
            # 那是平台特性不是不变式（曾在这里硬断言「必须有一个被忽略」，
            # 结果真实 CI（ubuntu）第一次跑就红了）。
            "rlimits_measured_in_child": disclosure["measured_not_empty"],
            "rlimits_at_least_one_enforced": disclosure["at_least_one_enforced"],
            "rlimits_ignored_set_exact": disclosure["ignored_set_exact"],
            "rlimit_note_matches_data": disclosure["note_matches_data"],
            "fs_boundary_not_overclaimed": env_rec.enforcement["filesystem_boundary"]
            .startswith("declared-only"),
            "network_boundary_not_overclaimed": env_rec.enforcement["network_boundary"]
            .startswith("declared-only"),
            "container_seam_exists": "--network=none" in container
            and "--read-only" in container,
            "span_chain_ok": report["spans"]["chain"]["ok"],
            "three_spans": len(report["spans"]["span_names"]) == 3,
        }
        return all(checks.values()), {
            "summary": f"隔离证据 {sum(checks.values())}/{len(checks)}："
                       f"rlimits {len(env_rec.rlimits_effective)}/"
                       f"{len(env_rec.rlimits_requested)} 实测生效，"
                       f"平台忽略 {','.join(env_rec.rlimits_ignored) or '无'}；"
                       f"fs/网络边界如实标为 declared-only",
            "checks": checks,
            "rlimits": {
                "requested": env_rec.rlimits_requested,
                "effective": env_rec.rlimits_effective,
                "ignored": list(env_rec.rlimits_ignored),
                "note": env_rec.enforcement["rlimits"],
            },
            "dropped_env_count": len(env_rec.dropped_env),
            "sensitive_dropped_count": len(env_rec.sensitive_dropped),
            "timeout": {"ms": slow_rec.duration_ms, "exit_code": slow_rec.exit_code},
            "enforcement": env_rec.enforcement,
            "span_summary": report["spans"],
            "container_command_head": list(container[:10]),
        }

    # ---- 13. [P5] 可观测链与预算熔断 ----
    def check_telemetry_budget(self):
        work = self.tmp / "runtime" / f"budget-{time.time_ns()}"
        py = sys.executable
        harness = RunHarness(
            work, "p5-budget", policy=SandboxPolicy(timeout_seconds=5.0),
            budget=Budget(max_actions=2, max_seconds=30,
                           max_external_requests=5, max_cost_usd=1.0),
        )
        first = harness.execute("one", [py, "-S", "-c", "print(1)"])
        second = harness.execute(
            "two", [py, "-S", "-c", "print(2)"],
            charge=Charge(input_tokens=1200, output_tokens=300,
                          seconds=0.4, external_requests=1))
        trip = sticky = None
        try:
            harness.execute("three", [py, "-S", "-c", "print(3)"])
        except BudgetExceeded as exc:
            trip = exc.reason
        try:
            harness.guard.check("four")
        except BudgetExceeded as exc:
            sticky = exc.reason
        report = harness.close()

        # 观测链必须能被证伪：改一行 -> 校验失败，且拒绝在坏链上续写。
        spans_path = Path(report["spans"]["log_path"])
        tampered = work / "spans-tampered.jsonl"
        lines = spans_path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["payload"]["span"]["name"] = "TAMPERED-BY-GATE"
        lines[0] = json.dumps(row, sort_keys=True)
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
        broken = SpanLog(tampered)
        chain_after = broken.verify_chain()
        append_rejected = False
        try:
            broken.append({"name": "should-not-append"})
        except RuntimeError:
            append_rejected = True

        cost_guard = BudgetGuard(Budget(max_cost_usd=0.000001),
                                work / "cost.jsonl", "cost")
        cost_reason = None
        try:
            cost_guard.check("big", Charge(input_tokens=100000, output_tokens=100000))
        except BudgetExceeded as exc:
            cost_reason = exc.reason
        req_guard = BudgetGuard(Budget(max_external_requests=1),
                                work / "req.jsonl", "req")
        req_reason = None
        try:
            req_guard.check("many", Charge(external_requests=2))
        except BudgetExceeded as exc:
            req_reason = exc.reason

        export = harness.tracer.log.export_otel()
        checks = {
            "allowed_actions_ran": first.ok and second.ok,
            "action_limit_tripped": bool(trip) and trip.startswith("actions="),
            "kill_switch_sticky": bool(sticky) and "KILL_SWITCH" in sticky,
            "journal_replay_matches": report["budget_journal"]["matches_live"],
            "journal_chain_ok": report["budget_journal"]["ok"],
            "span_chain_ok": report["spans"]["chain"]["ok"],
            "error_span_recorded": report["spans"]["statuses"] == ["OK", "OK", "ERROR"],
            "tampering_detected": not chain_after.ok,
            "corrupt_chain_rejects_writes": append_rejected,
            "semconv_pinned": report["spans"]["semconv_version"] == SEMCONV_VERSION,
            "otel_export_shape": "resourceSpans" in export
            and bool(export["resourceSpans"][0]["scopeSpans"][0]["spans"]),
            "cost_limit_tripped": bool(cost_reason) and cost_reason.startswith("cost_usd="),
            "external_request_limit_tripped": bool(req_reason)
            and req_reason.startswith("external_requests="),
        }
        return all(checks.values()), {
            "summary": f"span={report['spans']['span_count']} 条 / "
                       f"chain={report['spans']['chain']['ok']}，"
                       f"篡改检出={not chain_after.ok}，kill_switch={report['kill_switch_engaged']}，"
                       f"账本重放一致={report['budget_journal']['matches_live']}",
            "checks": checks,
            "trip_reason": trip, "sticky_reason": sticky,
            "cost_trip": cost_reason, "request_trip": req_reason,
            "spans": report["spans"],
            "budget_journal": report["budget_journal"],
            "tamper_verdict": chain_after.to_dict(),
            "semconv_version": SEMCONV_VERSION,
        }

    def check_real_model(self):
        """P6：真实模型冒烟。没有配置密钥就**跳过**（CI 不依赖密钥与外网）。

        跳过不是「通过」：报告会单独列出 skipped 项，让人一眼看出这项没被验过。
        """
        import os

        from michelin.models import OpenAICompatModel, resolve_api_key

        base = os.getenv("MW_API_BASE", "https://api.deepseek.com/v1")
        try:
            resolve_api_key(base)
        except RuntimeError as exc:
            return True, {
                "skipped": True,
                "reason": str(exc),
                "summary": "未配置模型密钥 → 跳过真实模型冒烟（CI 不依赖密钥）",
            }
        model = OpenAICompatModel(
            os.getenv("MW_WORKER_MODEL", "deepseek-flash"), api_base=base, max_tokens=256
        )
        response = model.complete("只回答一个词：OK")
        # 推理模型会把 max_tokens 先花在 reasoning 上：**预算太小会让正文为空**，
        # 而且是概率性的（这次推理短就过、下次全用完就挂）—— 门禁不能是随机的。
        # max_tokens 给足，并把「空正文」和「模型没答」分成两种可诊断的失败。
        ok = bool(response.text.strip()) and response.usage.total_tokens > 0
        evidence = {
            "summary": (f"{model.version} 调用成功：{response.usage.total_tokens} tokens "
                        f"(in={response.usage.input_tokens} out={response.usage.output_tokens} "
                        f"reasoning={response.usage.reasoning_tokens}) "
                        f"request_id={response.provider_request_id[:12]}"),
            "model": model.version,
            "usage": response.usage.to_dict(),
            "provider_request_id": response.provider_request_id,
            "finish_reason": response.finish_reason,
            "text_preview": response.text[:80],
        }
        if not ok:
            if not response.text.strip():
                evidence["reason"] = (
                    f"模型返回空正文（finish_reason={response.finish_reason!r}，"
                    f"reasoning={response.usage.reasoning_tokens}）："
                    "max_tokens 被推理吃光，探针预算不足"
                )
                evidence["summary"] = (f"{model.version} 调用了但正文为空 "
                                        f"(reasoning={response.usage.reasoning_tokens}, "
                                        f"finish_reason={response.finish_reason})")
            else:
                evidence["reason"] = "模型没有回报 token 用量"
            return False, evidence

        # 第二问：真实模型能不能产出 P2 worker 需要的**可机械检查**格式？
        # 这是真实模型路径最容易出问题的地方（模型可能回 6%、回整段文件、
        # 回没有赋值行的话），连通性通过不代表这一层通过。
        from michelin.workers.git_worker import _RATE_RE, _has_percent_suffix

        probe = OpenAICompatModel(
            os.getenv("MW_WORKER_MODEL", "deepseek-flash"), api_base=base, max_tokens=512
        )
        p2 = probe.complete(
            "只回答一行赋值，格式为 `TAX_RATE = <数值>`。\n"
            "数值是小数比例（6% 写成 0.06），不要带百分号、不要带其它内容。\n"
            "现在把税率改成 6%。"
        )
        match = _RATE_RE.search(p2.text)
        rate = match.group(1) if match else ""
        percent = bool(match) and _has_percent_suffix(p2.text, match)
        in_range = bool(match) and float(rate) <= 1
        p2_ok = bool(match) and not percent and in_range
        evidence["p2_format_probe"] = {
            "model": probe.version,
            "text_preview": p2.text[:120].replace("\n", "\\n"),
            "parsed_rate": rate,
            "percent_suffix": percent,
            "in_range": in_range,
            "ok": p2_ok,
            "usage": p2.usage.to_dict(),
        }
        if not p2_ok:
            evidence["reason"] = (
                f"真实模型未能产出可机械检查的 TAX_RATE 赋值（parsed={rate!r} "
                f"percent={percent} in_range={in_range}）"
            )
            evidence["summary"] += f"；⚠️ P2 输出格式探针失败（parsed={rate!r}）"
            return False, evidence
        evidence["summary"] += f"；P2 输出格式探针通过（TAX_RATE = {rate}）"

        # 第三问：真实模型能否按多文件 EDIT 协议产出？协议不通用 = 多文件路径不可用。
        from michelin.workers.editing_worker import parse_actions

        edit_probe = OpenAICompatModel(
            os.getenv("MW_WORKER_MODEL", "deepseek-flash"), api_base=base, max_tokens=600
        )
        ep = edit_probe.complete(
            "按下面的协议，把 shop/pricing.py 里的税率改成 0.06。只输出协议块，不要解释。\n"
            "=== BEGIN FILE: shop/pricing.py ===\n"
            "TAX_RATE = 0.06\n"
            "=== END FILE ==="
        )
        try:
            action = parse_actions(ep.text)
            edit_paths = [e.path for e in action.edits]
            edit_ok = bool(action.edits) and all(
                e.path.startswith("shop/") for e in action.edits
            )
            edit_reason = ""
        except Exception as exc:  # 解析失败也是「协议不可用」
            edit_paths, edit_ok = [], False
            edit_reason = f"{type(exc).__name__}: {exc}"
        evidence["p7_edit_protocol_probe"] = {
            "model": edit_probe.version,
            "text_preview": ep.text[:160].replace("\n", "\\n"),
            "parsed_paths": edit_paths,
            "ok": edit_ok,
            "reason": edit_reason,
            "usage": ep.usage.to_dict(),
        }
        if not edit_ok:
            evidence["reason"] = (
                "真实模型未能按多文件 EDIT 协议产出可解析的编辑块"
                + (f"：{edit_reason}" if edit_reason else "")
            )
            evidence["summary"] += "；⚠️ P7 EDIT 协议探针失败"
            return False, evidence
        evidence["summary"] += f"；P7 EDIT 协议探针通过（{edit_paths}）"
        return ok, evidence

    def check_no_secret_in_repo(self):
        """P6：仓库里不能出现任何密钥字面量（真实模型接入后的必检项）。"""
        import re

        patterns = {
            "openai/deepseek style": re.compile(r"sk-[A-Za-z0-9]{20,}"),
            "github classic PAT": re.compile(r"ghp_[A-Za-z0-9]{20,}"),
            "private key block": re.compile(r"BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY"),
        }
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "-c", "core.quotepath=false", "ls-files"],
            capture_output=True, text=True,
        ).stdout.splitlines()
        offenders: list[dict] = []
        for rel in tracked:
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for label, pattern in patterns.items():
                if pattern.search(text):
                    offenders.append({"file": rel, "pattern": label})
                    break
        return not offenders, {
            "summary": (f"扫描 {len(tracked)} 个受版本控制的文件，"
                        f"{len(offenders)} 处疑似密钥"),
            "scanned": len(tracked),
            "offenders": offenders,
        }

    def run(self) -> dict:
        started = datetime.now(timezone.utc).isoformat()
        print(f"P5 CI gate | root={ROOT} | fast={self.fast}")
        print(f"ast-grep   | {self.exe or 'NOT FOUND'}\n")
        self._check("unit_suite", self.check_unit)
        self._check("self_plan", self.check_self)
        self._check("external_plan", self.check_external)
        self._check("external_astgrep", self.check_external_astgrep)
        self._check("autodraft", self.check_autodraft)
        self._check("no_app_knowledge", self.check_no_app_knowledge)
        self._check("rules_in_ci", self.check_rules_in_ci)
        self._check("pr_rules_scan", self.check_pr_rules)
        self._check("negative_gate", self.check_negative)
        self._check("head_sha_sensitivity", self.check_head_sha)
        self._check("pr_diff_gate", self.check_pr_diff)
        self._check("runtime_isolation", self.check_runtime_isolation)
        self._check("telemetry_budget", self.check_telemetry_budget)
        self._check("no_secret_in_repo", self.check_no_secret_in_repo)
        self._check("real_model_smoke", self.check_real_model)

        failed = [c["name"] for c in self.checks if not c["ok"]]
        skipped = [c["name"] for c in self.checks if c.get("skipped")]
        report = {
            "gate": "michelin-p5-ci",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "root": str(ROOT),
            "python": sys.version.split()[0],
            "ast_grep": self.exe,
            "mode": "fast" if self.fast else "full",
            "fixture_repo": str(FIXTURE),
            "checks": self.checks,
            "summary": {
                "total": len(self.checks),
                "passed": len(self.checks) - len(failed) - len(skipped),
                "failed": failed,
                "skipped": skipped,
                "ok": not failed,
            },
            "report_hash_input": content_hash({
                "checks": [{"name": c["name"], "ok": c["ok"]} for c in self.checks],
            }),
        }
        print(f"\n{'GATE GREEN' if not failed else 'GATE RED'}: "
              f"{report['summary']['passed']}/{report['summary']['total']} 项通过"
              + (f"，跳过：{', '.join(skipped)}" if skipped else "")
              + (f"，失败：{', '.join(failed)}" if failed else ""))
        return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P5 CI 门禁唯一入口")
    ap.add_argument("--fast", action="store_true", help="跳过 head_sha 重型检查")
    ap.add_argument("--no-require-ast-grep", action="store_true",
                    help="本机没有 ast-grep 时降级为 skip（CI 不要用）")
    ap.add_argument("--report", default=str(ROOT / "data" / "ci" / "gate-report.json"))
    args = ap.parse_args(argv)
    gate = Gate(fast=args.fast, require_ast_grep=not args.no_require_ast_grep)
    report = gate.run()
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告        : {out}")
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
