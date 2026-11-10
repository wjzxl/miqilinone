"""命令行入口：P0 全链路 + P1 持久化脊 / webhook / durable workflow
+ P2 行为验证 + P3 可配置仓库访谈器 / 结构化规则门禁
+ P4 自动起草 plan + P5 运行时隔离 / 观测 / 预算熔断。

默认路径零外部依赖（SQLite + scripted 模型 + 本机 git + 纯标准库 AST）。
`--store postgres` 切到真实 Postgres；`p1-durable` 需要 .venv 里的 dbos；
装了 .venv 里的 ast-grep 时，P3 会自动升级成双后端对拍。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .chief.policy import TaskIntent
from .chief.workflow import ChiefOfStaff
from .context.sources import SourcePlane
from .delivery.replay import format_replay, replay
from .models import build_models
from .platform.store import EventStore

DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "data" / "workbench.db")

SCENARIOS: dict[str, tuple[list[str], str, str]] = {
    "ok": (
        ["EVIDENCE:ok 依据 source_hash 指向的一手原文完成只读分析，未改写任何段落。"],
        "ACCEPT", "",
    ),
    "flaky": (
        [
            "草稿：还没写证据标记，门禁应当拦下。",
            "EVIDENCE:ok 返工后补上证据标记，并注明来源哈希。",
        ],
        "ACCEPT", "",
    ),
    "always_fail": (
        ["草稿：始终不写证据标记。"],
        "ACCEPT", "",
    ),
    "sampling_reject": (
        ["EVIDENCE:ok 只读分析完成。"],
        "REJECT", "missing_evidence_marker",
    ),
}


def _store(path: str, backend: str = "sqlite", dsn: str | None = None):
    if backend == "postgres":
        from .platform.postgres import PostgresEventStore
        return PostgresEventStore(dsn)
    return EventStore(path)


def cmd_demo(args: argparse.Namespace) -> int:
    script, outcome, root_cause = SCENARIOS[args.scenario]
    store = _store(args.db, args.store, args.dsn)
    worker_model, verifier_model = build_models(
        backend=args.backend, worker_script=script, verifier_script=["PASS"]
    )
    chief = ChiefOfStaff(
        store, SourcePlane(store), worker_model, verifier_model, max_rework=args.max_rework
    )
    intent = TaskIntent(
        intent="只读分析：归纳这批一手材料的关键结论，结论必须可追溯到 source_hash",
        source_bodies=(
            "【需求原文 1】希望把验证循环做成工程门禁，而不是让模型自称完成。",
            "【需求原文 2】出错时改环境规则，不改提示词；规则需证明会被触发。",
        ),
        source_system="manual-cli",
    )
    result = chief.handle(intent, sampling_outcome=outcome, sampling_root_cause=root_cause)
    view = replay(store, result.run_id)

    print(f"场景          : {args.scenario}")
    print(f"run_id        : {result.run_id}")
    print(f"最终状态      : {result.state.value}")
    print(f"裁决          : {result.verdict or '无（已升级人工）'}")
    print(f"执行次数      : {result.attempts} (返工上限 {args.max_rework})")
    print(f"产物哈希      : {(result.artifact_hash or '')[:24]}")
    print(f"回流规则      : {result.rule_ref or '无'}")
    print(f"模型家族      : worker={worker_model.family} / verifier={verifier_model.family}")
    print(f"事件数        : {len(view.timeline)} | 哈希链 {'完好' if view.chain_ok else '损坏'}"
          f" | 六锚点 {'齐全' if view.complete else '缺失'}")
    print(f"库内容        : {view.counts}")
    print(f"\n回放命令      : python3 run.py replay --run-id {result.run_id}")
    return 0 if view.complete and view.chain_ok else 1


def cmd_replay(args: argparse.Namespace) -> int:
    store = _store(args.db, args.store, args.dsn)
    view = replay(store, args.run_id)
    print(format_replay(view))
    return 0 if view.complete else 1


def cmd_verify_chain(args: argparse.Namespace) -> int:
    store = _store(args.db, args.store, args.dsn)
    ok, count, bad = store.verify_chain()
    print(f"事件 {count} 条 | 哈希链 {'完好' if ok else f'损坏于 seq={bad}'}")
    print(f"库内容: {store.counts()}")
    return 0 if ok else 1


def cmd_rules(args: argparse.Namespace) -> int:
    store = _store(args.db, args.store, args.dsn)
    rules = store.list_rules()
    if not rules:
        print("暂无环境规则。跑 demo --scenario sampling_reject 可生成一条。")
    for r in rules:
        print(f"{r['rule_id']} | {r['kind']} -> {r['target']} | enabled={bool(r['enabled'])}"
              f" | mutation={r['mutation_test_ref'][:16]}")
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    from .models import ScriptedModel
    from .verification.verifier import IndependentVerifier

    store = _store(args.db, args.store, args.dsn)
    verifier = IndependentVerifier(store, ScriptedModel("openai", "cli-verifier", ["PASS"]))
    before = store.active_verdict(args.head_sha)
    verifier.invalidate(args.head_sha, args.run_id, args.reason)
    after = store.active_verdict(args.head_sha)
    print(f"作废前有效裁决: {before['verdict_id'] if before else '无'}")
    print(f"作废后有效裁决: {after['verdict_id'] if after else '无（已作废）'}")
    ok, count, _ = store.verify_chain()
    print(f"追加作废记录后哈希链 {'完好' if ok else '损坏'}，共 {count} 条事件")
    return 0 if after is None else 1


def _make_chief(store, script=None):
    from .models import ScriptedModel
    return ChiefOfStaff(
        store, SourcePlane(store),
        ScriptedModel("anthropic", "claude-stub-p1",
                      script or ["EVIDENCE:ok 依据一手原文完成只读分析。"]),
        ScriptedModel("openai", "gpt-stub-p1", ["PASS"]),
    )


def cmd_p1_webhook(args: argparse.Namespace) -> int:
    """端到端演示：验签 → 原子占位 → 立刻 200 → 异步处理 → 重发去重 → DLQ 重试。"""
    import json
    import time

    from .inbox.dlq import reconcile, retry_dlq
    from .inbox.handler import InlineRunner, receive_webhook
    from .inbox.objects import InboxObjectStore
    from .inbox.signature import sign

    store = _store(args.db, args.store, args.dsn)
    objects = InboxObjectStore(Path(args.db).parent / "objects")
    secret = os.getenv("MW_WEBHOOK_SECRET", "michelin-p1-secret")
    raw = json.dumps({
        "intent": "来自 webhook 的只读分析需求",
        "sources": ["原文一：验证循环要工程化。", "原文二：出错改环境规则。"],
    }, ensure_ascii=False).encode("utf-8")

    real = lambda: _make_chief(store)  # noqa: E731
    runner = InlineRunner(store, real)

    # DLQ 演示独立成段，用单独的 runner 制造一次下游故障，不污染主线投递
    if args.simulate_failure:
        calls = {"n": 0}

        def failing():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟下游故障（投递处理失败）")
            return real()

        fail_runner = InlineRunner(store, failing)
        fail_headers = {"X-Delivery-Id": "dlq-001", **sign(secret, raw)}
        receive_webhook(store, raw, fail_headers, secret=secret, runner=fail_runner,
                        objects=objects, source="generic")
        fail_runner.wait()
        before = store.get_delivery("dlq-001")
        print(f"故障投递  : delivery_id=dlq-001 status={before['status']}"
              f" reason={before['failure_reason']}")
        retried = retry_dlq(store, real, objects)
        after = store.get_delivery("dlq-001")
        print(f"DLQ 重试  : {retried} -> status={after['status']}")

    h1 = {"X-Delivery-Id": args.delivery_id, **sign(secret, raw)}
    r1 = receive_webhook(store, raw, h1, secret=secret, runner=runner,
                         objects=objects, source="generic")
    r2 = receive_webhook(store, raw, h1, secret=secret, runner=runner,
                         objects=objects, source="generic")
    print(f"首次投递  : {r1.status_code} accepted={r1.accepted} | {r1.reason}")
    print(f"重发同一ID: {r2.status_code} duplicate={r2.duplicate} | {r2.reason}")

    bad = receive_webhook(
        store, raw,
        {"X-Delivery-Id": "bad-sig", "X-Michelin-Timestamp": str(int(time.time())),
         "X-Michelin-Signature": "sha256=deadbeef"},
        secret=secret, runner=runner, objects=objects,
    )
    old = receive_webhook(
        store, raw,
        {"X-Delivery-Id": "replay-old", **sign(secret, raw, timestamp=int(time.time()) - 9999)},
        secret=secret, runner=runner, objects=objects,
    )
    print(f"坏签名    : {bad.status_code} | {bad.reason}")
    print(f"过期时间戳: {old.status_code} | {old.reason}")

    runner.wait()
    print(f"后台结果  : {runner.results}")
    print(f"投递台账  : {[(d['delivery_id'], d['status'], d['replay_count']) for d in store.list_deliveries()]}")
    print(f"对账      : {reconcile(store, {args.delivery_id, 'never-delivered'})}")
    print(f"事件链    : {store.verify_chain()} | 库内容 {store.counts()}")
    return 0


def cmd_p1_durable(args: argparse.Namespace) -> int:
    """两段式演示 DBOS：start（可模拟硬崩溃）→ resume（自动恢复 + 审批门）。"""
    import json
    import threading

    from .chief import durable_workflow as dw

    store = _store(args.db, args.store, args.dsn)
    if args.crash_at:
        os.environ["MW_CRASH_STEP"] = args.crash_at
    else:
        os.environ.pop("MW_CRASH_STEP", None)

    dw.configure_durable(store, dsn=args.dsn, phase=args.phase,
                         approval_timeout=args.approval_timeout)
    payload = {
        "intent": "P1 durable workflow 只读分析",
        "sources": ["原文：崩溃恢复必须跳过已完成 step，不能重新调模型。"],
        "source": "webhook",
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if args.phase == "start":
        wfid = start_wfid = dw.start_durable(args.workflow_id, payload, raw)
        print(f"workflow_id={wfid} 已启动（DBOS workflow_id = delivery_id）", flush=True)
        if not args.crash_at:
            timer = threading.Timer(1.5, lambda: dw.send_approval(wfid, args.decision))
            timer.daemon = True
            timer.start()
        result = dw.workflow_result(start_wfid)
    else:
        wfid = args.workflow_id
        timer = threading.Timer(2.0, lambda: dw.send_approval(wfid, args.decision))
        timer.daemon = True
        timer.start()
        result = dw.workflow_result(wfid)

    view = replay(store, result["run_id"])
    print(f"phase     : {args.phase}")
    print(f"workflow  : {wfid}")
    print(f"结果      : state={result['state']} verdict={result['verdict']} approved={result['approved']}")
    print(f"run_id    : {result['run_id']}")
    print(f"回放      : 事件 {len(view.timeline)} 条 | 六锚点 {'齐全' if view.complete else '缺失'}"
          f" | 哈希链 {'完好' if view.chain_ok else '损坏'}")
    print(f"状态路径  : {view.decision.get('state_path')}")
    print(f"事件链    : {store.verify_chain()} | 库内容 {store.counts()}")
    print(f"审批门    : {[(g['gate_id'], g['status'], g['decision']) for g in _gates(store, result['run_id'])]}")
    return 0 if view.complete and view.chain_ok else 1


def _gates(store, run_id: str) -> list[dict]:
    try:
        cur = store.conn.cursor()
        cur.execute("SELECT * FROM approval_gate WHERE run_id=%s ORDER BY created_at", (run_id,))
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return [dict(r) for r in store.conn.execute(
            "SELECT * FROM approval_gate WHERE run_id=? ORDER BY created_at", (run_id,)
        )]


P2_SCENARIOS: dict[str, dict] = {
    "ok": {"rates": ("0.06",)},
    "wrong_fix": {"rates": ("0.08", "0.06")},
    "always_fail": {"rates": ("0.09",)},
    "blind_gate": {"rates": ("0.06",), "weaken_drive": True},
    "evidence_eaten": {"rates": ("0.06",), "cleanup_eats_evidence": True},
    "sampling_reject": {"rates": ("0.06",), "outcome": "REJECT",
                        "root_cause": "verification_rule_gap"},
}


def cmd_p2_verify(args: argparse.Namespace) -> int:
    """P2：生成六段式 skill → 真跑证明 → 代码级变异门禁 → 真实 diff → 行为裁决。"""
    from .chief.p2_workflow import P2Pipeline, P2Scenario
    from .models import ScriptedModel
    from .platform.envelope import sha256_text

    cfg = P2_SCENARIOS[args.scenario]
    store = _store(args.db, args.store, args.dsn)
    worker_model = ScriptedModel("anthropic", "claude-stub-p2",
                                 ["将税率改为 TAX_RATE = 0.06。"])
    verifier_model = ScriptedModel("openai", "gpt-stub-p2", ["PASS"])
    scenario = P2Scenario(
        worker_rates=cfg["rates"],
        weaken_drive=cfg.get("weaken_drive", False),
        cleanup_eats_evidence=cfg.get("cleanup_eats_evidence", False),
        sampling_outcome=cfg.get("outcome", "ACCEPT"),
        sampling_root_cause=cfg.get("root_cause", ""),
    )
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pipeline = P2Pipeline(store, SourcePlane(store), worker_model, verifier_model,
                          workdir, max_rework=args.max_rework)
    intent = TaskIntent(
        intent="修复 shop 结算税率：total 10 20 应输出 TOTAL=31.80，且 receipt.json 与 stdout 一致",
        source_bodies=(
            "【需求原文】税率应为 6%，金额保留两位小数；验收必须驱动真实 CLI，不许读源码断言。",
            "【治理原文】每条门禁都要有故意破坏的变体证明它会开火；cleanup 不得删除证据。",
        ),
        source_system="manual-cli-p2",
    )
    result = pipeline.handle(intent, scenario)
    view = replay(store, result.run_id)

    types = [t["type"] for t in view.timeline]
    print(f"场景            : {args.scenario}")
    print(f"run_id          : {result.run_id}")
    print(f"最终状态        : {result.state.value}")
    print(f"裁决            : {result.verdict or '无（阻断/升级人工）'}")
    print(f"执行次数        : {result.attempts} (返工上限 {args.max_rework})")
    print(f"skill 状态      : {result.skill_status} | hash={result.skill_hash[:24]}")
    print(f"变异门禁全开火  : {result.mutants_all_fired}")
    print(f"head_sha        : {(result.head_sha or '')[:24] or '-'}")
    print(f"回流规则        : {result.rule_ref or '无'}")
    if result.blocked_reason:
        print(f"阻断原因        : {result.blocked_reason}")
    print(f"事件            : {len(view.timeline)} 条 | 六锚点 {'齐全' if view.complete else '不完整'}"
          f" | 哈希链 {'完好' if view.chain_ok else '损坏'}")
    print(f"关键事件        : {[t for t in dict.fromkeys(types) if t.startswith(('skill.', 'artifact.', 'verdict.'))]}")
    print(f"生成物目录      : {result.workdir}")
    print(f"skill 目录      : {result.workdir / 'skills' / '.cursor' / 'skills' / 'verify-shop'}")
    print(f"回放命令        : python3 run.py replay --run-id {result.run_id}")

    # 生成物可复核：把 SKILL.md 的内容哈希单独打出来，便于和事件里的 skill_hash 对照。
    skill_md = result.workdir / "skills" / ".cursor" / "skills" / "verify-shop" / "SKILL.md"
    if skill_md.exists():
        print(f"SKILL.md sha256 : {sha256_text(skill_md.read_text(encoding='utf-8'))[:24]}")
    return 0 if view.chain_ok and (result.state.value in {
        "ACCEPTED", "RULE_UPDATED", "NEEDS_HUMAN", "BLOCKED_NEEDS_HUMAN",
    }) else 1


PROJECT_ROOT = Path(__file__).resolve().parents[2]

P3_SCENARIOS: dict[str, dict] = {
    "ok": {},
    "unproven": {"cleanup_eats_evidence": True},
    "blind_gate": {"weaken_rules": True},
    "unbacked_plan": {"unbacked_plan": True},
    "backend_disagree": {"force_backend_disagreement": True},
    "sampling_reject": {
        "sampling_outcome": "REJECT",
        "sampling_root_cause": "missing_evidence_marker",
    },
}


def cmd_p3_interview(args: argparse.Namespace) -> int:
    """只读访谈一个仓库，并把 plan 拿回来做 grounded 校验。"""
    from .verification.interview import describe, ground_plan, interview
    from .verification.plan import load_plan

    profile = interview(args.repo)
    print(f"仓库            : {profile.root}")
    print(describe(profile))
    if args.plan:
        plan = load_plan(args.plan)
        problems = ground_plan(profile, plan)
        print(f"\n计划            : {args.plan}")
        print(f"plan_fingerprint: {plan.fingerprint()[:24]}")
        print(f"journeys={len(plan.journeys)} rules={len(plan.rules)} "
              f"mutations={len(plan.mutations)} features={len(plan.features)}")
        print(f"grounded        : {'是' if not problems else '否'}")
        for p in problems:
            print(f"  ! {p}")
        return 0 if not problems else 1
    return 0


def cmd_p3_verify(args: argparse.Namespace) -> int:
    """P3：访谈 → 生成通用六段式 skill → 双后端规则复核 → 变异门禁 → 裁决。"""
    from .chief.p3_workflow import P3Pipeline, P3Scenario
    from .models import ScriptedModel
    from .verification.plan import load_plan
    from .verification.rules import find_ast_grep

    cfg = P3_SCENARIOS[args.scenario]
    store = _store(args.db, args.store, args.dsn)
    plan = load_plan(args.plan)
    verifier_model = ScriptedModel("openai", "gpt-stub-p3", ["PASS"])
    scenario = P3Scenario(**cfg)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if args.rules_backend == "ast-grep" and not find_ast_grep(args.ast_grep):
        print("错误：--rules-backend ast-grep 但本机找不到可执行文件；"
              "可先 .venv/bin/pip install ast-grep-cli，或改用 --rules-backend python-ast",
              file=sys.stderr)
        return 2

    pipeline = P3Pipeline(
        store, SourcePlane(store), verifier_model,
        Path(args.repo), plan, workdir,
        backend=args.rules_backend, ast_grep=args.ast_grep,
        probe_drive=not args.no_drive_probe,
    )
    intent = TaskIntent(
        intent="为指定仓库生成一套可配置、经变异证明的六段式验证门禁并独立裁决",
        source_bodies=(
            "【需求原文】验证装置不得内置目标应用知识；命令、旅程、规则全部来自仓库访谈与验证计划。",
            "【治理原文】结构化规则需双后端对拍；每条规则要经故意违规证明会开火；出错改环境规则。",
        ),
        source_system="manual-cli-p3",
    )
    result = pipeline.handle(intent, scenario)
    view = replay(store, result.run_id)
    types = [t["type"] for t in view.timeline]

    print(f"场景            : {args.scenario}")
    print(f"run_id          : {result.run_id}")
    print(f"最终状态        : {result.state.value}")
    print(f"裁决            : {result.verdict or '无（阻断/升级人工）'}")
    print(f"被验证仓库      : {Path(args.repo).resolve()}")
    print(f"规则后端        : {result.backend} | 双后端对拍={'是' if result.cross_checked else '否'}")
    print(f"访谈指纹        : {result.repo_fingerprint[:24]}")
    print(f"计划指纹        : {result.plan_fingerprint[:24]}")
    print(f"skill_hash      : {result.skill_hash[:24] or '-'} | 状态={result.skill_status}")
    print(f"规则变异全开火  : {result.rules_all_fired}")
    print(f"head_sha        : {(result.head_sha or '')[:24] or '-'}")
    print(f"回流规则        : {result.rule_ref or '无'}")
    if result.blocked_reason:
        print(f"阻断原因        : {result.blocked_reason}")
    print(f"事件            : {len(view.timeline)} 条 | 六锚点 {'齐全' if view.complete else '不完整'}"
          f" | 哈希链 {'完好' if view.chain_ok else '损坏'}")
    print(f"关键事件        : {[t for t in dict.fromkeys(types) if t.startswith(('repo.', 'plan.', 'skill.', 'rules.', 'artifact.', 'verdict.'))]}")
    print(f"生成物目录      : {result.workdir}")
    if result.skill_root:
        print(f"skill 目录      : {result.skill_root}")
    print(f"回放命令        : python3 run.py replay --run-id {result.run_id}")
    return 0 if view.chain_ok and result.state.value in {
        "ACCEPTED", "RULE_UPDATED", "BLOCKED_NEEDS_HUMAN", "NEEDS_HUMAN",
    } else 1


def cmd_p4_draft(args: argparse.Namespace) -> int:
    """P4：只读访谈任意 Python 仓库，自动起草一份可执行的验证计划。"""
    from .verification.autodraft import describe_draft, draft_plan
    from .verification.interview import ground_plan
    from .verification.plan import dump_plan

    result = draft_plan(args.repo, app_name=args.app_name)
    print(f"目标仓库        : {result.profile.root}")
    print(describe_draft(result))
    problems = ground_plan(result.profile, result.plan)
    print(f"\n接地校验        : {'通过' if not problems else '未通过: ' + '; '.join(problems)}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump_plan(result.plan), encoding="utf-8")
        print(f"计划已写入      : {out}")
    print("下一步          : python3 run.py p3-verify --repo <仓库> --plan <上面的计划>")
    return 0 if not problems else 1


def cmd_p5_runtime(args: argparse.Namespace) -> int:
    """P5：本地跑一次「预算 → 隔离 → 观测」演示，产出 runtime-report.json。"""
    import json
    import time

    from .runtime import Budget, BudgetExceeded, Charge, RunHarness, SandboxPolicy

    work = Path(args.workdir) / f"run-{int(time.time())}"
    source = work / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "context.py").write_text("VALUE = 1\n", encoding="utf-8")
    harness = RunHarness(
        work / "run", "cli-p5",
        policy=SandboxPolicy(timeout_seconds=5.0),
        budget=Budget(max_actions=int(args.max_actions)),
    )
    py = sys.executable
    harness.execute("env", [py, "-S", "-c",
                            "import os,json;print(json.dumps(sorted(os.environ)))"])
    harness.execute(
        "read-source",
        [py, "-S", "-c",
         "import os;print(open(os.environ['MW_SOURCE_ROOT']+'/context.py').read().strip())"],
        source_root=source,
        charge=Charge(input_tokens=512, output_tokens=128, seconds=0.2),
    )
    harness.execute("slow", [py, "-S", "-c", "import time;time.sleep(10)"],
                   policy=SandboxPolicy(timeout_seconds=0.6))
    trips: list[str] = []
    if args.trip:
        for label in ("extra-1", "extra-2"):
            try:
                harness.execute(label, [py, "-S", "-c", "print(1)"])
            except BudgetExceeded as exc:
                trips.append(exc.reason)
    report = harness.close()

    print(f"运行目录        : {work / 'run'}")
    print(f"span            : {report['spans']['span_count']} 条，链完好="
          f"{report['spans']['chain']['ok']}，semconv={report['spans']['semconv_version']}")
    print(f"rlimits         : {report['receipts'][0]['rlimits_effective']} "
          f"(平台忽略 {report['receipts'][0]['rlimits_ignored']})")
    print(f"边界声明        : fs={report['receipts'][0]['enforcement']['filesystem_boundary']} / "
          f"net={report['receipts'][0]['enforcement']['network_boundary']}")
    print(f"预算账本        : 重放一致={report['budget_journal']['matches_live']}，"
          f"kill_switch={report['kill_switch_engaged']}")
    if trips:
        print(f"熔断演示        : {json.dumps(trips, ensure_ascii=False)}")
    print(f"完整证据        : {(work / 'run' / 'runtime-report.json')}")
    print("下一步          : python3 ci/gate.py（把同一套断言放进 13 项 CI 门禁）")
    return 0 if report["spans"]["chain"]["ok"] and report["budget_journal"]["matches_live"] else 1


def cmd_p1_status(args: argparse.Namespace) -> int:
    store = _store(args.db, args.store, args.dsn)
    rows = store.list_deliveries()
    print(f"投递台账  : {len(rows)} 条")
    for d in rows:
        print(f"  {d['delivery_id']:<28} {d['status']:<10} replay={d['replay_count']}"
              f" workflow={d['workflow_id'] or '-'}")
    print(f"事件链    : {store.verify_chain()}")
    print(f"库内容    : {store.counts()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="michelin",
                                     description="AI 米其林智能工作台 P0–P5")
    parser.add_argument("--db", default=os.getenv("MW_DB", DEFAULT_DB), help="SQLite 事件库路径")
    parser.add_argument("--store", choices=["sqlite", "postgres"],
                        default=os.getenv("MW_STORE", "sqlite"), help="事件存储后端")
    parser.add_argument("--dsn", default=os.getenv("MW_DATABASE_URL"),
                        help="Postgres DSN（--store postgres 时生效）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="跑通一个只读任务全链路")
    p_demo.add_argument("--scenario", choices=sorted(SCENARIOS), default="ok")
    p_demo.add_argument("--backend", default=os.getenv("MW_MODEL_BACKEND", "scripted"))
    p_demo.add_argument("--max-rework", type=int, default=2)
    p_demo.set_defaults(func=cmd_demo)

    p_replay = sub.add_parser("replay", help="按 run_id 只读回放因果链")
    p_replay.add_argument("--run-id", required=True)
    p_replay.set_defaults(func=cmd_replay)

    p_chain = sub.add_parser("verify-chain", help="校验 append-only 事件的全局哈希链")
    p_chain.set_defaults(func=cmd_verify_chain)

    p_rules = sub.add_parser("rules", help="列出已回流的 L6 环境规则")
    p_rules.set_defaults(func=cmd_rules)

    p_inv = sub.add_parser("invalidate", help="演示新 push 作废旧 verdict")
    p_inv.add_argument("--run-id", required=True)
    p_inv.add_argument("--head-sha", required=True)
    p_inv.add_argument("--reason", default="新 push，旧裁决作废")
    p_inv.set_defaults(func=cmd_invalidate)

    p_wh = sub.add_parser("p1-webhook", help="P1：验签 / 去重 / 防重放 / DLQ 重试演示")
    p_wh.add_argument("--delivery-id", default="delivery-p1-001")
    p_wh.add_argument("--simulate-failure", action="store_true", help="模拟一次下游故障并走 DLQ 重试")
    p_wh.set_defaults(func=cmd_p1_webhook)

    p_du = sub.add_parser("p1-durable", help="P1：DBOS 持久工作流 / 崩溃恢复 / 审批门")
    p_du.add_argument("--phase", choices=["start", "resume"], default="start")
    p_du.add_argument("--workflow-id", default="wf-p1-001")
    p_du.add_argument("--crash-at", choices=["execute"], help="start 阶段在指定 step 硬崩溃")
    p_du.add_argument("--decision", default="APPROVED")
    p_du.add_argument("--approval-timeout", type=float, default=30.0)
    p_du.set_defaults(func=cmd_p1_durable)

    p_st = sub.add_parser("p1-status", help="P1：查看投递台账与事件链")
    p_st.set_defaults(func=cmd_p1_status)

    p_p2 = sub.add_parser("p2-verify", help="P2：六段式 verify skill / 真实 git diff / 代码级变异门禁")
    p_p2.add_argument("--scenario", choices=sorted(P2_SCENARIOS), default="ok")
    p_p2.add_argument("--workdir",
                      default=os.getenv("MW_P2_WORKDIR",
                                        str(Path(__file__).resolve().parents[2] / "data" / "p2-runs")),
                      help="临时 git 仓库与生成 skill 的落盘目录")
    p_p2.add_argument("--max-rework", type=int, default=2)
    p_p2.set_defaults(func=cmd_p2_verify)

    p_p3i = sub.add_parser("p3-interview", help="P3：只读访谈任意仓库并校验计划是否接地")
    p_p3i.add_argument("--repo", default=str(PROJECT_ROOT), help="被访谈的仓库根目录")
    p_p3i.add_argument("--plan", help="可选：要校验的 plan.json")
    p_p3i.set_defaults(func=cmd_p3_interview)

    p_p3 = sub.add_parser("p3-verify", help="P3：可配置验证计划 / 双后端结构化规则 / 变异门禁")
    p_p3.add_argument("--repo", default=str(PROJECT_ROOT), help="被验证的仓库根目录")
    p_p3.add_argument("--plan", default=str(PROJECT_ROOT / "plans" / "michelin-self.json"),
                      help="验证计划 JSON")
    p_p3.add_argument("--scenario", choices=sorted(P3_SCENARIOS), default="ok")
    p_p3.add_argument("--rules-backend", choices=["python-ast", "ast-grep"],
                      default="python-ast")
    p_p3.add_argument("--ast-grep", help="ast-grep 可执行文件路径（默认自动探测）")
    p_p3.add_argument("--no-drive-probe", action="store_true",
                      help="变异测试只跑规则引擎、不跑生成的 Drive（更快，但证据较弱）")
    p_p3.add_argument("--workdir",
                      default=os.getenv("MW_P3_WORKDIR",
                                        str(PROJECT_ROOT / "data" / "p3-runs")),
                      help="生成 skill 与 run 证据的落盘目录")
    p_p3.set_defaults(func=cmd_p3_verify)

    p_p4d = sub.add_parser("p4-draft", help="P4：只读访谈任意仓库并自动起草验证计划")
    p_p4d.add_argument("--repo", default=str(PROJECT_ROOT), help="被访谈的仓库根目录")
    p_p4d.add_argument("--app-name", help="plan.app_name，默认取仓库目录名")
    p_p4d.add_argument("--out", help="把草稿 plan.json 写到指定路径")
    p_p4d.set_defaults(func=cmd_p4_draft)

    p_p5 = sub.add_parser("p5-runtime", help="P5：隔离执行 / span 链 / 预算熔断本地演示")
    p_p5.add_argument("--workdir",
                      default=str(PROJECT_ROOT / "data" / "p5-runs"),
                      help="运行证据落盘目录")
    p_p5.add_argument("--max-actions", type=int, default=3, help="本次 run 的动作数上限")
    p_p5.add_argument("--trip", action="store_true",
                       help="额外请求动作，演示黏性 kill switch")
    p_p5.set_defaults(func=cmd_p5_runtime)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # CLI 边界统一报错，库内异常保持原样抛出
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
