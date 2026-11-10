"""P3 的可配置验证计划：把「验证什么」从生成器的代码里搬到一份 JSON 契约里。

P2 的 SkillSpec 把 shop 应用的命令、期望输出、feature 文案全部硬编码在 Python 里，
换一个仓库就要改生成器。P3 把它拆成两层：

* RepoInterview（interview.py）：确定性扫描仓库，产出事实，不猜测、不调模型。
* VerificationPlan（本模块）：人/上游 Agent 声明的验收契约 —— 启动、体检、
  若干用户旅程、结构化代码规则、以及每条规则对应的「故意违规」变异。

设计约束（与 pstack create-verification-skill §2 的 "grounded in what the interview
actually found (no placeholders left)" 对齐）：
* plan 里所有 argv 必须是真实可执行的命令，生成器不做任何占位符替换；
* 每条规则必须配一条 mutation，否则规则未经证明、不得启用；
* plan 整体算 fingerprint，绑进 skill_hash 与 verdict —— 改契约就要重裁。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..platform.envelope import content_hash

RULE_KINDS: tuple[str, ...] = (
    "forbidden_call",       # 禁止调用某个函数/方法（可含点号）
    "forbidden_import",     # 禁止 import 某个模块（含 from ... import）
    "forbidden_literal",    # 禁止把某个全局名赋成指定字面量
)

SEVERITIES: tuple[str, ...] = ("error", "warning", "hint")


@dataclass(frozen=True, slots=True)
class Command:
    """一条真实命令。argv 不用 shell，杜绝 `shell=True` 与注入面。"""

    argv: tuple[str, ...]
    expect_stdout_contains: str = ""
    expect_exit_code: int = 0
    timeout_seconds: int = 60

    def to_dict(self) -> dict:
        return {**asdict(self), "argv": list(self.argv)}

    @staticmethod
    def from_dict(raw: dict) -> "Command":
        argv = tuple(str(a) for a in raw.get("argv", ()))
        if not argv:
            raise ValueError("Command.argv 不能为空（no placeholders left）")
        return Command(
            argv=argv,
            expect_stdout_contains=str(raw.get("expect_stdout_contains", "")),
            expect_exit_code=int(raw.get("expect_exit_code", 0)),
            timeout_seconds=int(raw.get("timeout_seconds", 60)),
        )


@dataclass(frozen=True, slots=True)
class Journey(Command):
    """Drive 段的一条用户旅程：动作 + 结果状态必须成对记录。"""

    name: str = ""

    def to_dict(self) -> dict:
        # 不能用 super().to_dict()：dataclass(slots=True) 会重建类对象，
        # 零参数 super() 捕获的 __class__ 指向装饰前的类，运行时会抛
        # "obj is not an instance or subtype of type"。这里显式展开字段。
        return {
            "argv": list(self.argv),
            "expect_stdout_contains": self.expect_stdout_contains,
            "expect_exit_code": self.expect_exit_code,
            "timeout_seconds": self.timeout_seconds,
            "name": self.name,
        }

    @staticmethod
    def from_dict(raw: dict) -> "Journey":
        base = Command.from_dict(raw)
        name = str(raw.get("name") or "journey")
        return Journey(
            argv=base.argv,
            expect_stdout_contains=base.expect_stdout_contains,
            expect_exit_code=base.expect_exit_code,
            timeout_seconds=base.timeout_seconds,
            name=name,
        )


@dataclass(frozen=True, slots=True)
class StructuredRule:
    """结构化代码规则（不是正则、不是字符串 contains）。

    selector 的含义随 kind 变化：
    * forbidden_call   -> 调用名，如 `eval` / `os.system`
    * forbidden_import -> 模块名，如 `requests`
    * forbidden_literal-> 变量名；literal 是源码字面量文本，如 `0.10`
    """

    id: str
    kind: str
    selector: str
    message: str
    globs: tuple[str, ...] = ("**/*.py",)
    literal: str = ""
    severity: str = "error"

    def to_dict(self) -> dict:
        return {**asdict(self), "globs": list(self.globs)}

    @staticmethod
    def from_dict(raw: dict) -> "StructuredRule":
        kind = str(raw.get("kind", ""))
        if kind not in RULE_KINDS:
            raise ValueError(f"未知规则类型 {kind!r}，可用：{RULE_KINDS}")
        rule = StructuredRule(
            id=str(raw.get("id") or ""),
            kind=kind,
            selector=str(raw.get("selector") or ""),
            message=str(raw.get("message") or ""),
            globs=tuple(str(g) for g in raw.get("globs", ("**/*.py",))),
            literal=str(raw.get("literal", "")),
            severity=str(raw.get("severity", "error")),
        )
        if not rule.id or not rule.selector or not rule.message:
            raise ValueError(f"规则 {rule.id!r} 缺 id/selector/message")
        if rule.severity not in SEVERITIES:
            raise ValueError(f"规则 {rule.id} 的 severity 非法：{rule.severity}")
        if rule.kind == "forbidden_literal" and not rule.literal:
            raise ValueError(f"规则 {rule.id} 是 forbidden_literal，必须给 literal")
        if not rule.globs:
            raise ValueError(f"规则 {rule.id} 必须指定 globs")
        return rule


@dataclass(frozen=True, slots=True)
class RuleMutation:
    """一条「故意违规」：往目标文件尾部注入 snippet，规则必须因此开火。

    这对应架构文档 §L6：规则双写 + 变异证明。没有变异证据的规则不启用，
    避免「写了一条永远不会失败的 lint」这种自欺。
    """

    rule_id: str
    inject_file: str
    snippet: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "RuleMutation":
        m = RuleMutation(
            rule_id=str(raw.get("rule_id") or ""),
            inject_file=str(raw.get("inject_file") or ""),
            snippet=str(raw.get("snippet") or ""),
        )
        if not (m.rule_id and m.inject_file and m.snippet.strip()):
            raise ValueError(f"变异 {m.rule_id!r} 缺 rule_id/inject_file/snippet")
        return m


@dataclass(frozen=True, slots=True)
class FeatureDoc:
    """feature map 的一个条目：四个固定 H2 的内容，全部来自 plan，无模板占位符。"""

    name: str
    sub_features: str
    user_pov: str
    driving: str
    gotchas: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "FeatureDoc":
        f = FeatureDoc(
            name=str(raw.get("name") or ""),
            sub_features=str(raw.get("sub_features") or ""),
            user_pov=str(raw.get("user_pov") or ""),
            driving=str(raw.get("driving") or ""),
            gotchas=str(raw.get("gotchas") or ""),
        )
        if not f.name or not all((f.sub_features, f.user_pov, f.driving, f.gotchas)):
            raise ValueError(f"feature {f.name!r} 四段内容不能为空（no placeholders left）")
        return f


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    app_name: str
    surface: str
    language: str
    launch: Command
    doctor: Command
    journeys: tuple[Journey, ...]
    rules: tuple[StructuredRule, ...]
    mutations: tuple[RuleMutation, ...]
    features: tuple[FeatureDoc, ...]
    evidence_path: str = "evidence/proof.json"
    isolate_note: str = "VERIFY_DATA_DIR 按 run 隔离"
    notes: tuple[str, ...] = field(default_factory=tuple)

    # ---- 校验：计划本身必须是可执行的，不能带着空壳进入生成 ----
    def validate(self) -> None:
        if not self.app_name or not self.surface:
            raise ValueError("plan 缺 app_name / surface")
        if self.language.lower() not in ("python",):
            raise ValueError(f"P3 结构化规则后端目前只支持 python，收到 {self.language!r}")
        if not self.journeys:
            raise ValueError("plan 至少要有一条 journey（Drive 必须驱动真实用户路径）")
        if not self.rules:
            raise ValueError("plan 至少要有一条结构化规则")
        if not self.features:
            raise ValueError("plan 至少要有一条 feature（feature map 不能是空目录）")
        ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("规则 id 重复")
        covered = {m.rule_id for m in self.mutations}
        missing = [r.id for r in self.rules if r.id not in covered]
        if missing:
            raise ValueError(f"这些规则没有变异证明，不得启用：{missing}")
        orphan = [m.rule_id for m in self.mutations if m.rule_id not in set(ids)]
        if orphan:
            raise ValueError(f"变异引用了不存在的规则：{orphan}")
        for cmd in (self.launch, self.doctor, *self.journeys):
            for arg in cmd.argv:
                if arg in ("&&", "||", "|", ";", ">", ">>"):
                    raise ValueError(f"argv 里出现 shell 元字符 {arg!r}，禁止拼 shell")

    def to_dict(self) -> dict:
        return {
            "app_name": self.app_name,
            "surface": self.surface,
            "language": self.language,
            "launch": self.launch.to_dict(),
            "doctor": self.doctor.to_dict(),
            "journeys": [j.to_dict() for j in self.journeys],
            "rules": [r.to_dict() for r in self.rules],
            "mutations": [m.to_dict() for m in self.mutations],
            "features": [f.to_dict() for f in self.features],
            "evidence_path": self.evidence_path,
            "isolate_note": self.isolate_note,
            "notes": list(self.notes),
        }

    def fingerprint(self) -> str:
        return content_hash(self.to_dict())

    @staticmethod
    def from_dict(raw: dict) -> "VerificationPlan":
        plan = VerificationPlan(
            app_name=str(raw.get("app_name") or ""),
            surface=str(raw.get("surface") or ""),
            language=str(raw.get("language") or "python"),
            launch=Command.from_dict(raw.get("launch") or {}),
            doctor=Command.from_dict(raw.get("doctor") or {}),
            journeys=tuple(Journey.from_dict(x) for x in raw.get("journeys", ())),
            rules=tuple(StructuredRule.from_dict(x) for x in raw.get("rules", ())),
            mutations=tuple(RuleMutation.from_dict(x) for x in raw.get("mutations", ())),
            features=tuple(FeatureDoc.from_dict(x) for x in raw.get("features", ())),
            evidence_path=str(raw.get("evidence_path") or "evidence/proof.json"),
            isolate_note=str(raw.get("isolate_note") or "VERIFY_DATA_DIR 按 run 隔离"),
            notes=tuple(str(n) for n in raw.get("notes", ())),
        )
        plan.validate()
        return plan


def load_plan(path: str | Path) -> VerificationPlan:
    return VerificationPlan.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def dump_plan(plan: VerificationPlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
