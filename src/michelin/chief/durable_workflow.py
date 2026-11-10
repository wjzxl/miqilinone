"""P1 真 durable workflow：DBOS（Postgres 检查点）+ 人工审批门 + 可复现崩溃恢复。

这个文件**只在 --durable 路径被 import**，顶层依赖 dbos；
P0 的零依赖路径完全不碰它。

设计纪律（对应架构文档的风险清单「Replay Divergence」）：
* 所有非确定性动作（生成 ID、调模型、写事件、取当前时间）**必须放在 @DBOS.step() 内**，
  workflow 体里只做编排与 DBOS 原语（recv）。恢复时已完成 step 直接返回 checkpoint 结果，
  绝不重新调模型、绝不重构出从未发生的状态链。
* step 里的外部副作用靠平台层的幂等键兜底：即使 step 被重放，events 也不会多出第二条。
* `DBOS.recv` / `DBOS.send` 提供 exactly-once 的人工审批门，消息存在 Postgres 里，
  崩溃重启也不会丢。

崩溃恢复演示：start 阶段设 MW_CRASH_STEP=execute 让进程在 step_execute 内 os._exit(137)
（模拟硬杀，不给 DBOS 清理机会）；resume 阶段不设该变量，DBOS.launch() 自动恢复
PENDING workflow，跳过已完成的 ingest/scope step，从 execute 继续。
"""

from __future__ import annotations

import os
from typing import Any

from dbos import DBOS, DBOSConfig, SetWorkflowID

from ..chief.policy import EvidenceSpec, Kickoff, Predicate, TaskState
from ..context.sources import SourcePlane
from ..delivery.sampler import Sampler
from ..models import ScriptedModel
from ..platform.envelope import SEMCONV_VERSION, content_hash, make_envelope, new_id, utcnow
from ..verification.holdout import build_holdout
from ..verification.verifier import IndependentVerifier
from ..workers.base import ReadonlyWorker

DEFAULT_DSN = "postgresql://michelin:michelin@127.0.0.1:55432/michelin"
GOVERNANCE_DOCS = (
    "只读任务：不写任何文件、不调用写工具",
    "证据规格必须可检查，禁止以模型自述作为完成判据",
    "独立验证者不得看到作者 plan / transcript",
)

_CONTEXT: dict[str, Any] = {}


def _store():
    return _CONTEXT["store"]


def _emit(run_id: str, task_id: str, event_type: str, payload: dict, **fields: object) -> None:
    _store().emit(
        make_envelope(
            run_id=run_id, task_id=task_id,
            actor=str(fields.pop("actor", "chief-of-staff")),
            role=str(fields.pop("role", "chief")),
            event_type=event_type,
            **fields,
        ),
        payload,
    )


def _transition(run_id: str, task_id: str, src: str, dst: str, why: str) -> None:
    _store().set_task_status(task_id, dst)
    _emit(run_id, task_id, "state.transition", {"from": src, "to": dst, "why": why})


def _maybe_crash(step_name: str) -> None:
    if os.getenv("MW_CRASH_STEP") == step_name:
        print(f"CRASH_SIMULATED at {step_name} (os._exit 137)", flush=True)
        os._exit(137)


# ---------- DBOS steps：每个 step 是一个可恢复检查点 ----------

@DBOS.step()
def step_ingest(delivery_id: str, payload: dict, raw_text: str) -> dict:
    store = _store()
    run_id, task_id = new_id("run"), new_id("task")
    intent = str(payload.get("intent") or "外部投递的只读分析需求")
    marker = str(payload.get("evidence_marker", "EVIDENCE:ok"))

    store.put_inbox_event(delivery_id, str(payload.get("source", "webhook")), True, payload)
    doc = SourcePlane(store).ingest(
        raw_text, source_system="webhook-raw", source_uri=f"inbox://{delivery_id}"
    )
    store.put_task({
        "task_id": task_id, "parent_id": None, "intent": intent,
        "source_refs": [doc.source_hash], "evidence_spec": {},
        "risk_level": str(payload.get("risk_level", "low")),
        "budget": float(payload.get("budget", 5.0)),
        "status": TaskState.INBOX_RECEIVED.value, "created_at": utcnow(),
    })
    _emit(run_id, task_id, "intent.received", {
        "intent": intent, "delivery_id": delivery_id,
    })
    _emit(run_id, task_id, "context.sourced", {
        "source_hashes": [doc.source_hash], "kind": "一手原文（原始投递全文，非摘要）",
    })
    _transition(run_id, task_id, TaskState.INBOX_RECEIVED.value, TaskState.NORMALIZED.value,
                "需求已标准化")
    return {
        "delivery_id": delivery_id, "run_id": run_id, "task_id": task_id,
        "intent": intent, "marker": marker,
        "source_hashes": [doc.source_hash], "raw_text": raw_text,
    }


@DBOS.step()
def step_scope(ctx: dict) -> dict:
    store = _store()
    spec = EvidenceSpec((
        Predicate("evidence_marker_present", "diff_contains", ctx["marker"]),
        Predicate("independent_gate_green", "test_output_contains", "INDEPENDENT_PASS"),
        Predicate("no_author_plan_leak", "diff_absent", "AUTHOR_PLAN"),
        Predicate("readonly_governance", "governance_contains", "只读"),
    ))
    # 显式重建 task 行，避免把已序列化的 JSON 字段二次编码
    store.put_task({
        "task_id": ctx["task_id"], "parent_id": None, "intent": ctx["intent"],
        "source_refs": ctx["source_hashes"], "evidence_spec": spec.to_dict(),
        "risk_level": "low", "budget": 5.0,
        "status": TaskState.SCOPED.value, "created_at": utcnow(),
    })
    _transition(ctx["run_id"], ctx["task_id"], TaskState.NORMALIZED.value,
                TaskState.SCOPED.value, "已写出可验证证据规格")
    _emit(ctx["run_id"], ctx["task_id"], "task.scoped", {
        "evidence_spec": spec.to_dict(), "writable_paths": [], "unit": "一个可验证单元",
    })
    return {**ctx, "evidence_spec": spec.to_dict()}


@DBOS.step()
def step_execute(ctx: dict) -> dict:
    _maybe_crash("execute")
    store = _store()
    spec = EvidenceSpec.from_dict(ctx["evidence_spec"])
    docs = [SourcePlane(store).fetch(h) for h in ctx["source_hashes"]]
    kickoff = Kickoff(
        task_id=ctx["task_id"], intent=ctx["intent"],
        source_hashes=tuple(ctx["source_hashes"]), writable_paths=(),
        forbidden_actions=("write_file", "network_egress", "shell_exec"),
        evidence_spec=spec, risk_level="low", budget=5.0, attempt=1,
    )
    worker = ReadonlyWorker(_CONTEXT["worker_model"], evidence_marker=ctx["marker"])
    output = worker.run(kickoff, docs)
    worker_run_id = new_id("run")
    store.put_agent_run({
        "run_id": worker_run_id, "task_id": ctx["task_id"], "actor": "readonly-worker",
        "role": "author", "model_family": output.model_response.model_family,
        "model_version": output.model_response.model_version,
        "prompt_hash": output.model_response.prompt_hash,
        "semconv_version": SEMCONV_VERSION,
        "tool_policy": {"readonly": True}, "started_at": utcnow(),
    })
    _emit(ctx["run_id"], ctx["task_id"], "worker.model_response", {
        "model_family": output.model_response.model_family,
        "model_version": output.model_response.model_version,
        "prompt_hash": output.model_response.prompt_hash,
        "tool_calls": list(output.tool_calls),
        "self_check_output": output.self_check_output,
    }, actor="readonly-worker", role="author", parent_id=worker_run_id,
        model_family=output.model_response.model_family,
        model_version=output.model_response.model_version,
        prompt_hash=output.model_response.prompt_hash)
    _transition(ctx["run_id"], ctx["task_id"], TaskState.SCOPED.value,
                TaskState.DISPATCHED.value, "派发")
    _transition(ctx["run_id"], ctx["task_id"], TaskState.DISPATCHED.value,
                TaskState.IN_PROGRESS.value, "worker 开始执行")
    _transition(ctx["run_id"], ctx["task_id"], TaskState.IN_PROGRESS.value,
                TaskState.SELF_CHECKED.value, "worker 自检完毕")

    artifact_hash = content_hash(output.artifact_body)
    store.put_artifact(artifact_hash, worker_run_id, f"artifact://{ctx['run_id']}/1", "inline:events")
    _emit(ctx["run_id"], ctx["task_id"], "artifact.produced", {
        "artifact_hash": artifact_hash, "attempt": 1,
    }, artifact_hash=artifact_hash)
    _transition(ctx["run_id"], ctx["task_id"], TaskState.SELF_CHECKED.value,
                TaskState.VERIFYING.value, "交给独立验证者")

    bundle = build_holdout(
        issue_body=ctx["intent"], artifact_text=output.artifact_body,
        self_reported_test_output=output.self_check_output,
        governance_docs=GOVERNANCE_DOCS,
    )
    verifier = IndependentVerifier(store, _CONTEXT["verifier_model"])
    verdict = verifier.verify(
        bundle, run_id=ctx["run_id"], task_id=ctx["task_id"],
        author_run_id=worker_run_id,
        author_family=output.model_response.model_family,
        head_sha=artifact_hash, evidence_spec=spec,
        source_hashes=tuple(ctx["source_hashes"]),
    )
    _transition(ctx["run_id"], ctx["task_id"], TaskState.VERIFYING.value,
                TaskState.VERDICT_PASS.value if verdict.result == "PASS"
                else TaskState.VERDICT_FAIL.value, verdict.result)
    return {**ctx, "verdict": verdict.result, "artifact_hash": artifact_hash}


@DBOS.step()
def step_open_gate(ctx: dict) -> dict:
    gate_id = new_id("gate")
    topic = f"approval:{ctx['delivery_id']}"
    _store().put_approval_gate({
        "gate_id": gate_id, "run_id": ctx["run_id"], "topic": topic,
        "status": "WAITING", "created_at": utcnow(),
    })
    _emit(ctx["run_id"], ctx["task_id"], "approval.requested", {
        "gate_id": gate_id, "topic": topic,
        "reason": "不可逆动作前的人工审批门（light factory）",
    })
    return {**ctx, "gate_id": gate_id, "topic": topic}


@DBOS.step()
def step_close_gate(ctx: dict, decision: Any) -> dict:
    approved = decision == "APPROVED"
    _store().decide_approval_gate(
        ctx["gate_id"], str(decision or "TIMEOUT"),
        "human-approver" if approved else "system-timeout",
    )
    _emit(ctx["run_id"], ctx["task_id"], "approval.decided", {
        "gate_id": ctx["gate_id"], "decision": decision, "approved": approved,
    })
    return {**ctx, "approved": approved, "decision": decision}


@DBOS.step()
def step_deliver(ctx: dict) -> dict:
    store = _store()
    if not ctx.get("approved"):
        _emit(ctx["run_id"], ctx["task_id"], "human.escalated", {
            "reason": f"审批未通过: {ctx.get('decision')}",
        })
        store.set_task_status(ctx["task_id"], TaskState.NEEDS_HUMAN.value)
        return {
            "run_id": ctx["run_id"], "task_id": ctx["task_id"],
            "state": TaskState.NEEDS_HUMAN.value, "verdict": ctx["verdict"],
            "artifact_hash": ctx["artifact_hash"], "approved": False,
        }
    _transition(ctx["run_id"], ctx["task_id"], TaskState.VERDICT_PASS.value,
                TaskState.DELIVERED.value, "人工审批通过")
    _emit(ctx["run_id"], ctx["task_id"], "artifact.delivered", {
        "artifact_hash": ctx["artifact_hash"],
    })
    _transition(ctx["run_id"], ctx["task_id"], TaskState.DELIVERED.value,
                TaskState.SAMPLED.value, "进入抽样验收")
    sampler = Sampler(store, EvidenceSpec.from_dict(ctx["evidence_spec"]), ctx["marker"])
    sampler.review(
        run_id=ctx["run_id"], task_id=ctx["task_id"],
        outcome="ACCEPT", head_sha=ctx["artifact_hash"],
    )
    _transition(ctx["run_id"], ctx["task_id"], TaskState.SAMPLED.value,
                TaskState.ACCEPTED.value, "抽样通过")
    return {
        "run_id": ctx["run_id"], "task_id": ctx["task_id"],
        "state": TaskState.ACCEPTED.value, "verdict": ctx["verdict"],
        "artifact_hash": ctx["artifact_hash"], "approved": True,
    }


@DBOS.workflow()
def michelin_workflow(delivery_id: str, payload: dict, raw_text: str) -> dict:
    """workflow 体里只有编排 + DBOS 原语；非确定性全部关在 step 里。"""
    ctx = step_ingest(delivery_id, payload, raw_text)
    ctx = step_scope(ctx)
    ctx = step_execute(ctx)
    ctx = step_open_gate(ctx)
    # 人工审批门：消息存 Postgres，崩溃重启后仍能收到，exactly-once
    decision = DBOS.recv(ctx["topic"], timeout_seconds=_CONTEXT["approval_timeout"])
    ctx = step_close_gate(ctx, decision)
    return step_deliver(ctx)


# ---------- 对外接口 ----------

def configure_durable(
    store: Any,
    *,
    dsn: str | None = None,
    worker_model: Any | None = None,
    verifier_model: Any | None = None,
    approval_timeout: float = 30.0,
    phase: str = "start",
) -> None:
    """必须在调用任何 workflow 之前、且每进程只调用一次。"""
    _CONTEXT.update({
        "store": store,
        "worker_model": worker_model or ScriptedModel(
            "anthropic", "claude-stub-p1", ["EVIDENCE:ok 持久工作流产出，引用 source_hash。"]
        ),
        "verifier_model": verifier_model or ScriptedModel("openai", "gpt-stub-p1", ["PASS"]),
        "approval_timeout": approval_timeout,
        "phase": phase,
    })
    # 同一 Postgres 系统库里 application_version 必须跨应用唯一；
    # 崩溃恢复时 resume 必须用完全相同的版本戳，否则 DBOS 不会接管 PENDING workflow。
    config: DBOSConfig = {
        "name": "michelin-workbench",
        "application_version": os.getenv("MW_APP_VERSION", "michelin-p1-0.1.0"),
        "system_database_url": dsn or os.getenv("MW_DATABASE_URL", DEFAULT_DSN),
    }
    DBOS(config=config)
    DBOS.launch()


def start_durable(delivery_id: str, payload: dict, raw: bytes) -> str:
    """用 delivery_id 作 workflow_id —— webhook 重发时 DBOS 天然 exactly-once。"""
    with SetWorkflowID(delivery_id):
        handle = DBOS.start_workflow(
            michelin_workflow, delivery_id, payload, raw.decode("utf-8", errors="replace")
        )
    return handle.get_workflow_id()


def send_approval(workflow_id: str, decision: str) -> None:
    DBOS.send(workflow_id, decision, topic=f"approval:{workflow_id}")


def workflow_result(workflow_id: str) -> dict:
    handle = DBOS.retrieve_workflow(workflow_id)
    return dict(handle.get_result())


class DBOSRuntime:
    """实现 DurableRuntime 协议，供 handler / CLI 统一调用。"""

    def __init__(self, store: Any, **config: Any):
        configure_durable(store, **config)
        self.store = store

    def start(self, delivery_id: str, payload: dict) -> str:
        import json
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return start_durable(delivery_id, payload, raw)

    def send_approval(self, workflow_id: str, decision: str) -> None:
        send_approval(workflow_id, decision)

    def result(self, workflow_id: str) -> dict:
        return workflow_result(workflow_id)
