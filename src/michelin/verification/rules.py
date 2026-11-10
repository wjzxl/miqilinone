"""结构化代码规则的双后端引擎：ast-grep（真实 AST 匹配）+ 纯标准库 python-ast。

为什么两个后端：
* 生成的 verify skill 要能在冷启动、零 pip 依赖的机器上运行 —— helper 内嵌
  python-ast 检查器，不需要装任何东西。
* 但「自己写的 AST 检查器」容易自证，所以主流程可以用真正的 ast-grep 复核，
  并把两个后端对同一份代码的判定逐条对拍；不一致即视为规则定义有歧义，直接阻断。

ast-grep CLI 的真实行为（本机 0.51.1 实测，非文档猜测）：
    ast-grep scan --rule <one-rule.yml> --json <files...>
  命中 -> exit 1 + JSON 数组（含 file/line/text）；干净 -> exit 0 + `[]`。
  单规则模式不需要 sgconfig.yml。`sg` 是 deprecated 别名，必须用 `ast-grep`。
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from .plan import StructuredRule

BACKENDS = ("python-ast", "ast-grep")


@dataclass(frozen=True, slots=True)
class RuleFinding:
    rule_id: str
    file: str
    line: int
    column: int
    text: str
    backend: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuleScan:
    backend: str
    findings: tuple[RuleFinding, ...]
    files_scanned: tuple[str, ...]
    rules_applied: tuple[str, ...]
    scan_hash: str

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "findings": [f.to_dict() for f in self.findings],
            "files_scanned": list(self.files_scanned),
            "rules_applied": list(self.rules_applied),
            "scan_hash": self.scan_hash,
            "clean": self.clean,
        }


# --------------------------------------------------------------------------
# 通用：按 globs 收集候选文件（排除生成物与虚拟环境，避免扫到自己的 skill）
# --------------------------------------------------------------------------

SKIP_PARTS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", "data", ".workbuddy",
})


def collect_files(root: Path, globs: tuple[str, ...]) -> tuple[str, ...]:
    base = Path(root).resolve()
    found: set[str] = set()
    for pattern in globs:
        for p in base.glob(pattern):
            if not p.is_file():
                continue
            rel = p.relative_to(base)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            found.add(str(rel))
    return tuple(sorted(found))


# --------------------------------------------------------------------------
# 后端 A：python-ast（纯标准库，生成物内嵌的就是这一份语义）
# --------------------------------------------------------------------------

def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _matches_python_ast(rule: StructuredRule, tree: ast.AST, source: str) -> list[tuple[int, int, str]]:
    """只对命中的节点取源码片段。

    ast.get_source_segment 要按行列回切源码，对大文件很贵；早先把它放在
    每个候选节点上，4 条规则扫 58 个文件要 3.7s，命中才提取后降到毫秒级。
    forbidden_literal 例外：它必须读 value 的源码文本才能比较字面量。
    """
    hits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        matched = False
        if rule.kind == "forbidden_call" and isinstance(node, ast.Call):
            matched = _dotted(node.func) == rule.selector
        elif rule.kind == "forbidden_import":
            if isinstance(node, ast.Import):
                matched = any(a.name == rule.selector or a.name.startswith(rule.selector + ".")
                              for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                m = node.module or ""
                matched = m == rule.selector or m.startswith(rule.selector + ".")
        elif rule.kind == "forbidden_literal" and isinstance(node, ast.Assign):
            # 先做便宜的名字检查，只有名字对了才去切 value 的源码片段。
            if any(isinstance(t, ast.Name) and t.id == rule.selector for t in node.targets):
                seg = (ast.get_source_segment(source, node.value) or "").strip()
                matched = seg == rule.literal
        if matched:
            text = ast.get_source_segment(source, node) or ""
            hits.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0),
                         text.replace("\n", " ")[:200]))
    return sorted(hits)


# 变异测试会对同一棵代码树连扫多轮，只有被注入的那一个文件内容会变。
# 按 (路径, 内容哈希) 缓存解析结果，避免每轮都把 56 个文件重解析 4 次。
_PARSE_CACHE: dict[tuple[str, str], tuple[str, ast.AST]] = {}
_CACHE_LIMIT = 3000


def _parsed(path: Path, rel: str) -> tuple[str, ast.AST] | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    key = (str(path), hashlib.sha256(source.encode("utf-8")).hexdigest())
    cached = _PARSE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        value = (source, ast.parse(source, filename=rel))
    except (SyntaxError, ValueError):
        return None
    if len(_PARSE_CACHE) >= _CACHE_LIMIT:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[key] = value
    return value


def scan_python_ast(rules: tuple[StructuredRule, ...], root: Path) -> RuleScan:
    base = Path(root).resolve()
    findings: list[RuleFinding] = []
    all_files: set[str] = set()
    for rule in rules:
        files = collect_files(base, rule.globs)
        all_files.update(files)
        for rel in files:
            parsed = _parsed(base / rel, rel)
            if parsed is None:
                continue
            source, tree = parsed
            for line, col, text in _matches_python_ast(rule, tree, source):
                findings.append(RuleFinding(rule.id, rel, line, col, text, "python-ast"))
    ordered = tuple(sorted(findings, key=lambda f: (f.rule_id, f.file, f.line, f.column)))
    return RuleScan(
        backend="python-ast",
        findings=ordered,
        files_scanned=tuple(sorted(all_files)),
        rules_applied=tuple(r.id for r in rules),
        scan_hash=content_hash({"findings": [f.to_dict() for f in ordered],
                                "files": sorted(all_files)}),
    )


# --------------------------------------------------------------------------
# 后端 B：ast-grep（生成 YAML 规则，逐条调 CLI）
# --------------------------------------------------------------------------

def _yaml_escape(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def rule_to_yaml(rule: StructuredRule) -> str:
    """把一条 StructuredRule 编译成 ast-grep 单规则 YAML（字段已按官方文档核实）。"""
    if rule.kind == "forbidden_call":
        body = f"  pattern: {rule.selector}($$$ARGS)\n"
    elif rule.kind == "forbidden_import":
        regex = "^" + rule.selector.replace(".", "\\.") + "$"
        body = (
            "  any:\n"
            "    - all:\n"
            "        - pattern: import $MOD\n"
            "        - has:\n"
            "            kind: dotted_name\n"
            f"            regex: {regex}\n"
            "    - all:\n"
            "        - pattern: from $MOD import $$$NAMES\n"
            "        - has:\n"
            "            kind: dotted_name\n"
            f"            regex: {regex}\n"
        )
    elif rule.kind == "forbidden_literal":
        body = f"  pattern: {rule.selector} = {rule.literal}\n"
    else:  # pragma: no cover - plan.validate 已挡住
        raise ValueError(f"未知规则类型：{rule.kind}")
    return (
        f"id: {rule.id}\n"
        "language: Python\n"
        f"severity: {rule.severity}\n"
        f"message: {_yaml_escape(rule.message)}\n"
        "rule:\n" + body
    )


def find_ast_grep(explicit: str | None = None) -> str | None:
    """返回绝对路径：scan 会以仓库根为 cwd，相对路径会找不到可执行文件。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p.resolve())
        return shutil.which(explicit)
    found = shutil.which("ast-grep")
    if found:
        return found
    # 项目内隔离 venv 兜底：默认零依赖路径照常可用，装了就自动升级成双后端对拍。
    for candidate in (
        Path.cwd() / ".venv" / "bin" / "ast-grep",
        Path(__file__).resolve().parents[3] / ".venv" / "bin" / "ast-grep",
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def scan_ast_grep(
    rules: tuple[StructuredRule, ...],
    root: Path,
    *,
    executable: str | None = None,
) -> RuleScan:
    exe = find_ast_grep(executable)
    if not exe:
        raise RuntimeError("未找到 ast-grep 可执行文件；用 --rules-backend python-ast 或先安装")
    base = Path(root).resolve()
    findings: list[RuleFinding] = []
    all_files: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="mw-asg-") as tmp:
        for i, rule in enumerate(rules):
            yml = Path(tmp) / f"rule-{i}-{rule.id}.yml"
            yml.write_text(rule_to_yaml(rule), encoding="utf-8")
            files = collect_files(base, rule.globs)
            all_files.update(files)
            if not files:
                continue
            proc = subprocess.run(
                [exe, "scan", "--rule", str(yml), "--json", *files],
                cwd=str(base), capture_output=True, text=True, timeout=120,
            )
            if proc.returncode not in (0, 1):
                raise RuntimeError(
                    f"ast-grep 扫描规则 {rule.id} 失败 (exit={proc.returncode}): "
                    f"{proc.stderr.strip()[:300]}"
                )
            try:
                hits = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"ast-grep 输出的不是 JSON：{proc.stdout[:200]!r}") from exc
            for hit in hits:
                rel = hit.get("file", "")
                try:
                    rel = str(Path(rel).resolve().relative_to(base))
                except ValueError:
                    pass
                start = hit.get("range", {}).get("start", {})
                findings.append(RuleFinding(
                    rule_id=rule.id, file=rel,
                    line=int(start.get("line", 0)) + 1,
                    column=int(start.get("column", 0)) + 1,
                    text=str(hit.get("text", "")).replace("\n", " ")[:200],
                    backend="ast-grep",
                ))
    ordered = tuple(sorted(findings, key=lambda f: (f.rule_id, f.file, f.line, f.column)))
    return RuleScan(
        backend="ast-grep",
        findings=ordered,
        files_scanned=tuple(sorted(all_files)),
        rules_applied=tuple(r.id for r in rules),
        scan_hash=content_hash({"findings": [f.to_dict() for f in ordered],
                                "files": sorted(all_files)}),
    )


def scan(rules: tuple[StructuredRule, ...], root: Path, backend: str = "python-ast",
         *, executable: str | None = None) -> RuleScan:
    if backend not in BACKENDS:
        raise ValueError(f"未知后端 {backend!r}，可用 {BACKENDS}")
    if backend == "ast-grep":
        return scan_ast_grep(rules, root, executable=executable)
    return scan_python_ast(rules, root)


def compare_backends(left: RuleScan, right: RuleScan) -> tuple[bool, tuple[str, ...]]:
    """对拍：同一份代码、同一组规则，两个后端必须给出一致的「哪些规则被触发」。

    不比行列号（两个 AST 的行列口径略有差别），只比 (rule_id, file) 集合 ——
    这是裁决真正依赖的信息。任何分歧都说明规则定义有歧义，必须人工处理。
    """
    l = {(f.rule_id, f.file) for f in left.findings}
    r = {(f.rule_id, f.file) for f in right.findings}
    diff = tuple(sorted(f"{a}::{b}" for a, b in (l - r) | (r - l)))
    return (not diff, diff)


def rules_digest(rules: tuple[StructuredRule, ...]) -> str:
    return content_hash({r.id: rule_to_yaml(r) for r in rules})
