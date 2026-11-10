"""确定性仓库访谈器：不调模型、不猜命令，只从磁盘上读到的事实生成 RepoProfile。

对应 pstack create-verification-skill 的 "interview the repo" 那一步。P2 的
build_shop_spec() 把访谈结果写死在 Python 里；P3 让访谈成为可复用的组件，
并且把 plan 拿回来做 **grounded 校验**：计划里出现的测试目标/文件路径必须在
仓库里真实存在，否则「no placeholders left」只是口号。

访谈结果整体算 fingerprint 并写进事件链，因此「同一仓库 + 同一计划」的裁决
可复现；仓库一变，指纹就变，旧裁决自动失效。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..platform.envelope import content_hash
from .plan import VerificationPlan

EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "dist", "build", ".tox", "site-packages",
    "data", ".idea", ".vscode", "htmlcov", ".workbuddy",
})
MAX_FILES = 4000

LANG_EXT: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
}

GOVERNANCE_CANDIDATES = (
    "AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md",
    "pyproject.toml", "setup.cfg", "ruff.toml", ".flake8", "mypy.ini",
    "Makefile", "package.json", "go.mod", "pytest.ini", "tox.ini",
)

# P4 泛化：P3 只认 `tests/` 前缀，外部仓库用 `spec/` / `test/` 时会误报「0 个测试目标」。
# 改成按 pytest / unittest 的通用命名约定识别，与目录名无关。
def _is_test_target(rel: str) -> bool:
    name = Path(rel).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    )


def _detect_framework(base: Path, targets: tuple[str, ...], configured_pytest: bool) -> str:
    """看测试文件里真实 import/使用了什么，而不是只看有没有 pytest 配置段。

    ledger-kit 有 `[tool.pytest.ini_options]` 但整套测试是 unittest；只看配置会误判。
    """
    if not targets:
        return "none"
    uses_pytest = uses_unittest = False
    for rel in targets:
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        uses_pytest = uses_pytest or "pytest" in text
        uses_unittest = uses_unittest or "unittest" in text
    if uses_pytest and uses_unittest:
        return "pytest+unittest"
    if uses_pytest:
        return "pytest"
    if uses_unittest:
        return "unittest"
    return "pytest(configured)" if configured_pytest else "unknown"


@dataclass(frozen=True, slots=True)
class ModuleSummary:
    path: str
    classes: tuple[str, ...]
    functions: tuple[str, ...]
    decorators: tuple[str, ...]
    loc: int

    def to_dict(self) -> dict:
        return {**asdict(self), "classes": list(self.classes),
                "functions": list(self.functions), "decorators": list(self.decorators)}


@dataclass(frozen=True, slots=True)
class RepoProfile:
    root: str
    languages: dict[str, int]
    entrypoints: tuple[str, ...]
    test_targets: tuple[str, ...]
    test_framework: str
    governance_files: tuple[str, ...]
    modules: tuple[ModuleSummary, ...]
    tracked_files: tuple[str, ...]
    fingerprint: str

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "languages": self.languages,
            "entrypoints": list(self.entrypoints),
            "test_targets": list(self.test_targets),
            "test_framework": self.test_framework,
            "governance_files": list(self.governance_files),
            "modules": [m.to_dict() for m in self.modules],
            "tracked_files": list(self.tracked_files),
            "fingerprint": self.fingerprint,
        }


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < MAX_FILES:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in EXCLUDED_DIRS and not p.is_symlink():
                    stack.append(p)
            elif p.is_file() and not p.is_symlink():
                out.append(p)
    return out


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return type(node).__name__


def _summarise(path: Path, rel: str) -> ModuleSummary | None:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=rel)
    except (OSError, UnicodeError, SyntaxError):
        return None
    classes: list[str] = []
    functions: list[str] = []
    decorators: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
            decorators.extend(_decorator_name(d) for d in node.decorator_list)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            decorators.extend(_decorator_name(d) for d in node.decorator_list)
    return ModuleSummary(
        path=rel, classes=tuple(classes), functions=tuple(functions),
        decorators=tuple(sorted(set(decorators))), loc=len(text.splitlines()),
    )


def _pyproject_scripts(text: str) -> tuple[str, ...]:
    """不引入 tomllib 之外的依赖；Python 3.11+ 标准库自带 tomllib。"""
    import tomllib

    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError):
        return ()
    scripts = data.get("project", {}).get("scripts", {})
    return tuple(f"{name} = {spec}" for name, spec in sorted(scripts.items()))


def interview(root: str | Path) -> RepoProfile:
    """只读扫描一个仓库，产出可复现的访谈结果。"""
    base = Path(root).resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"不是目录：{base}")
    files = _walk(base)
    rels = sorted(str(p.relative_to(base)) for p in files)

    languages: dict[str, int] = {}
    for p in files:
        lang = LANG_EXT.get(p.suffix.lower())
        if lang:
            languages[lang] = languages.get(lang, 0) + 1

    pyproject = base / "pyproject.toml"
    entrypoints: list[str] = []
    if pyproject.is_file():
        entrypoints.extend(_pyproject_scripts(pyproject.read_text(encoding="utf-8")))
    for rel in rels:
        if rel.endswith("__main__.py"):
            entrypoints.append(rel)
        if rel.endswith("/cli.py") or rel == "cli.py":
            entrypoints.append(rel + " (CLI module)")

    test_targets = tuple(rel for rel in rels if _is_test_target(rel))
    has_pytest = any(
        (base / name).exists() for name in ("pytest.ini", "tox.ini")
    ) or (pyproject.is_file() and "[tool.pytest" in pyproject.read_text(encoding="utf-8"))
    framework = _detect_framework(base, test_targets, has_pytest)

    governance = tuple(rel for rel in GOVERNANCE_CANDIDATES if (base / rel).is_file())

    modules: list[ModuleSummary] = []
    for rel in rels:
        if not rel.endswith(".py"):
            continue
        summary = _summarise(base / rel, rel)
        if summary is not None:
            modules.append(summary)

    # 指纹只吃源码内容，不吃时间戳/绝对路径/git SHA —— 同样的内容必须得到同样的裁决。
    payload: dict[str, str] = {}
    for rel in rels:
        if rel.endswith((".py", ".toml", ".json", ".sql", ".md", ".yaml", ".yml")):
            try:
                payload[rel] = (base / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    fingerprint = content_hash(payload)

    return RepoProfile(
        root=str(base),
        languages=dict(sorted(languages.items())),
        entrypoints=tuple(sorted(set(entrypoints))),
        test_targets=test_targets,
        test_framework=framework,
        governance_files=governance,
        modules=tuple(modules),
        tracked_files=tuple(rels),
        fingerprint=fingerprint,
    )


def ground_plan(profile: RepoProfile, plan: VerificationPlan) -> tuple[str, ...]:
    """把 plan 里提到的测试目标/注入文件对照磁盘事实，返回不一致项（空 = 全部 grounded）。

    这一步是「访谈驱动生成」和「凭空编命令」的分界线：计划里写 tests.test_holdout，
    仓库里就必须真的有 tests/test_holdout.py；变异要注入的文件必须存在且是 Python 文件。
    """
    problems: list[str] = []
    known = set(profile.tracked_files)
    for cmd in (plan.launch, plan.doctor, *plan.journeys):
        for arg in cmd.argv:
            # unittest 的 dotted target，例如 tests.test_holdout
            if re.fullmatch(r"tests(?:\.[A-Za-z_][A-Za-z0-9_]*)+", arg):
                rel = arg.replace(".", "/") + ".py"
                if rel not in known:
                    problems.append(f"journey 引用的测试目标不存在：{arg} -> {rel}")
            # 显式路径 / discover 的裸文件名 pattern
            elif re.fullmatch(r"[\w./-]+\.(py|toml|json|sql|md|ya?ml)", arg):
                if "/" in arg:
                    if arg.lstrip("./") not in known and arg not in known:
                        problems.append(f"命令引用的文件不存在：{arg}")
                elif not any(Path(k).name == arg for k in known):
                    problems.append(f"命令引用的文件不存在：{arg}")
    for mutation in plan.mutations:
        if mutation.inject_file not in known:
            problems.append(f"变异注入目标不存在：{mutation.inject_file}")
        elif not mutation.inject_file.endswith(".py"):
            problems.append(f"变异注入目标不是 Python 文件：{mutation.inject_file}")
    for rule in plan.rules:
        for glob in rule.globs:
            if glob.startswith("/") or glob.startswith("../"):
                problems.append(f"规则 {rule.id} 的 glob 越出仓库：{glob}")
    return tuple(sorted(set(problems)))


def describe(profile: RepoProfile) -> str:
    return json.dumps({
        "root": profile.root,
        "languages": profile.languages,
        "entrypoints": list(profile.entrypoints),
        "test_framework": profile.test_framework,
        "test_targets": list(profile.test_targets)[:12],
        "governance_files": list(profile.governance_files),
        "python_modules": len(profile.modules),
        "tracked_files": len(profile.tracked_files),
        "fingerprint": profile.fingerprint[:20],
    }, ensure_ascii=False, indent=2)
