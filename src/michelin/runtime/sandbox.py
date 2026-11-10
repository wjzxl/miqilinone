"""每任务进程级隔离执行器（P5 / 架构 §3 L3 + §8 成本失控、上下文投毒）。

**先把边界说清楚，不假装做到了容器隔离**：

能强制（本机实测）：
* env 白名单 —— 宿主密钥 / DB DSN / 代理 token 一律不进子进程，子进程 HOME/TMPDIR
  指向沙箱私有目录；
* RLIMIT_CPU / AS / FSIZE / NOFILE —— 在 fork 后 exec 前设置，由子进程自己读回取证；
* wall-clock 超时 —— 超时按进程组 SIGKILL，不留孤儿；
* 源快照 —— 复制后 chmod 只读，任务不能改一手上下文。

不能强制（同 UID 的进程天然做不到）：文件系统越界写入、出站网络。子进程仍能写
宿主任意路径、仍能联网。因此 `IsolationReceipt.enforcement` 明确把这两项标成
`declared-only`，生产必须换 `container_command()` 生成的 gVisor/Kata/容器后端。
**把「声明」冒充「强制」比没有隔离更危险**，这也是 CI 检查项要断言的内容。

设计上刻意不引入 Docker 依赖：CI 在任何开发机上都能跑同一套门禁，容器只是生产接缝。
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ..platform.envelope import content_hash

GENESIS = "GENESIS"

SENSITIVE_HINTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "DATABASE_URL",
    "DSN", "AUTH", "COOKIE", "PRIVATE", "MW_", "AWS_", "GITHUB_",
)

# 只放行与语言运行时相关、且不含凭据的变量；其余一律丢弃（记录 key，不记录值）。
BASE_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SYSTEMROOT", "TMPDIR")


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    timeout_seconds: float = 30.0
    cpu_seconds: int = 10
    memory_mb: int = 1024
    file_size_mb: int = 32
    max_open_files: int = 256
    network: str = "off"                 # declared policy；进程后端无法强制
    backend: str = "process"             # process | container（后者只产生命令）
    env_allow: tuple[str, ...] = BASE_ENV_ALLOW
    snapshot_source: bool = True

    def rlimits(self) -> dict[str, int]:
        return {
            "RLIMIT_CPU": self.cpu_seconds,
            "RLIMIT_AS": self.memory_mb * 1024 * 1024,
            "RLIMIT_FSIZE": self.file_size_mb * 1024 * 1024,
            "RLIMIT_NOFILE": self.max_open_files,
        }

    def to_dict(self) -> dict:
        return {**asdict(self), "rlimits": self.rlimits(),
                "env_allow": list(self.env_allow)}


@dataclass(frozen=True, slots=True)
class IsolationReceipt:
    label: str
    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    duration_ms: float
    cwd: str
    home: str
    env_keys: tuple[str, ...]
    env_hash: str
    dropped_env: tuple[str, ...]
    sensitive_dropped: tuple[str, ...]
    rlimits_requested: dict[str, int]
    rlimits_effective: dict[str, int]
    rlimits_ignored: tuple[str, ...]
    source_fingerprint: str | None
    network_policy: str
    backend: str
    enforcement: dict[str, str]
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "argv": list(self.argv), "env_keys": list(self.env_keys),
            "dropped_env": list(self.dropped_env),
            "sensitive_dropped": list(self.sensitive_dropped),
            "ok": self.ok,
        }


def _copy_readonly(source: Path, dest: Path) -> str:
    """复制一手上下文为只读快照，返回按路径排序的内容指纹（不是目录 mtime）。"""
    shutil.copytree(
        source, dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules"),
    )
    digests: dict[str, str] = {}
    for f in sorted(dest.rglob("*")):
        if f.is_file():
            digests[str(f.relative_to(dest))] = hashlib.sha256(f.read_bytes()).hexdigest()
            os.chmod(f, 0o444)
        elif f.is_dir():
            os.chmod(f, 0o555)
    return content_hash(digests)


def _make_env(box: Path, policy: SandboxPolicy, extra_env: dict[str, str] | None,
              source: Path | None) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    env: dict[str, str] = {}
    dropped: list[str] = []
    sensitive: list[str] = []
    for key, value in os.environ.items():
        if key in policy.env_allow:
            env[key] = value
        else:
            dropped.append(key)
            if any(hint in key.upper() for hint in SENSITIVE_HINTS):
                sensitive.append(key)
    home = box / "home"
    tmp = box / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home), "TMPDIR": str(tmp),
        "PYTHONNOUSERSITE": "1",
        "MW_SANDBOX_ID": box.name,
        "MW_NETWORK_POLICY": policy.network,
    })
    if source is not None:
        env["MW_SOURCE_ROOT"] = str(source)
    for key, value in (extra_env or {}).items():
        env[key] = str(value)
    return env, tuple(sorted(dropped)), tuple(sorted(sensitive))


def _apply_rlimits(limits: dict[str, int]) -> dict[str, int]:
    """fork 后、exec 前执行；返回**实测读回值**，而不是「请求过就算生效」。

    macOS 会静默忽略 RLIMIT_AS（设置 1GB，读回 RLIM_INFINITY）。若只记录请求值，
    证据就会把「没强制」写成「已强制」，比没有隔离更危险。
    """
    effective: dict[str, int] = {}
    for name, value in limits.items():
        rid = getattr(resource, name, None)
        if rid is None:
            continue
        try:
            _soft, hard = resource.getrlimit(rid)
            # soft 只能往下降；hard 保持不变，避免请求值超过系统上限而报错。
            resource.setrlimit(rid, (min(value, hard if hard > 0 else value), hard))
            effective[name] = resource.getrlimit(rid)[0]
        except (ValueError, OSError, RuntimeError):
            continue
    return effective


def isolated_run(
    argv: tuple[str, ...] | list[str],
    *,
    label: str,
    workdir: Path,
    policy: SandboxPolicy | None = None,
    source_root: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> IsolationReceipt:
    """在一个私有 cwd / env / rlimits / 只读源快照里跑一次外部命令。"""
    policy = policy or SandboxPolicy()
    box = Path(workdir).resolve() / "box"
    box.mkdir(parents=True, exist_ok=True)
    source: Path | None = None
    fingerprint: str | None = None
    if source_root is not None and policy.snapshot_source:
        source = box / "src"
        if not source.exists():
            fingerprint = _copy_readonly(Path(source_root).resolve(), source)
        else:  # 同一 label 复用快照，指纹按当前文件内容重算
            digests = {str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest()
                       for f in sorted(source.rglob("*")) if f.is_file()}
            fingerprint = content_hash(digests)
    env, dropped, sensitive = _make_env(box, policy, extra_env, source)
    env_hash = content_hash(env)
    limits = policy.rlimits()
    args = [str(a) for a in argv]

    started = time.monotonic()
    probe = box / "rlimits-effective.json"
    if probe.exists():
        probe.unlink()
    preexec = None
    if os.name == "posix":
        def preexec() -> None:  # noqa: WPS430 - preexec_fn 必须是子进程内可调用对象
            os.setsid()
            effective = _apply_rlimits(limits)
            try:
                probe.write_text(json.dumps(effective), encoding="utf-8")
            except OSError:
                pass

    proc = subprocess.Popen(
        args, cwd=str(box), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, preexec_fn=preexec,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=policy.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
        else:
            proc.kill()
        stdout, stderr = proc.communicate()
    duration = (time.monotonic() - started) * 1000
    effective: dict[str, int] = {}
    try:
        if probe.exists():
            effective = json.loads(probe.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        effective = {}
    ignored = tuple(sorted(name for name, value in limits.items()
                          if effective.get(name) != value))
    enforced_count = len(limits) - len(ignored)
    return IsolationReceipt(
        label=label, argv=tuple(args), exit_code=proc.returncode,
        timed_out=timed_out, duration_ms=round(duration, 2),
        cwd=str(box), home=env["HOME"],
        env_keys=tuple(sorted(env)), env_hash=env_hash,
        dropped_env=dropped, sensitive_dropped=sensitive,
        rlimits_requested=limits, rlimits_effective=effective,
        rlimits_ignored=ignored, source_fingerprint=fingerprint,
        network_policy=policy.network, backend=policy.backend,
        enforcement={
            "env_whitelist": "enforced",
            "cwd_and_home": "enforced",
            "rlimits": (f"measured-in-child: {enforced_count}/{len(limits)} enforced"
                        + (f"; platform ignored: {','.join(ignored)}" if ignored else "")),
            "wall_clock_timeout": "enforced (SIGKILL process group)",
            "source_snapshot": "enforced (read-only copy)" if source else "not-requested",
            "filesystem_boundary": "declared-only (needs container backend)",
            "network_boundary": "declared-only (needs container backend)",
        },
        stdout=stdout[-4000:], stderr=stderr[-2000:],
    )


def container_command(argv: tuple[str, ...] | list[str], *, policy: SandboxPolicy,
                      source_root: Path, workdir: Path) -> tuple[str, ...]:
    """生产接缝：生成等价的容器命令（本函数不执行，CI 也不依赖 Docker）。

    用 `--network=none` + `--read-only` + `--cap-drop=ALL` 把本机声明的边界变成强制边界；
    真正上线还要把 runtime 换成 gVisor/Kata、加 seccomp profile 与非 root 用户。
    """
    return (
        "docker", "run", "--rm", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "128", "--memory", f"{policy.memory_mb}m",
        "--cpus", "1",
        "-v", f"{Path(source_root).resolve()}:/app:ro",
        "-v", f"{Path(workdir).resolve()}:/work",
        "-w", "/work", "-e", f"MW_NETWORK_POLICY={policy.network}",
        "python:3.13-slim", *[str(a) for a in argv],
    )
