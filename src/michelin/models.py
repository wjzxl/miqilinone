"""模型适配层：把「脑」与「脊」分开，并强制记录 prompt_hash / model_version。

后端：
- `scripted`（默认）：零依赖、确定性，今天就能跑通全链路。
- `openai_compat`：任何 OpenAI 兼容的 /chat/completions。
- `deepseek`：DeepSeek 官方端点（https://api.deepseek.com/v1），模型
  `deepseek-flash` / `deepseek-v4-pro`。

**密钥只从环境变量或本机文件读，绝不入仓**：优先 `MW_API_KEY`，
其次 `MW_API_KEY_FILE`，再退到 `~/.config/michelin/<provider>_api_key`。
错误信息里永远不含密钥。

**模型家族独立性**：worker 与 verifier 必须分属不同家族（pstack holdout）。
DeepSeek 的两个模型同属一家，因此默认会被拒绝；确实要用同一家的模型时，
必须显式声明豁免（`MW_ALLOW_SAME_MODEL_FAMILY=1`），并且这份豁免会被
**写进证据链**，不允许静默降级。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .platform.envelope import sha256_text


def family_of(model_name: str) -> str:
    name = model_name.lower()
    if "claude" in name:
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4", "codex")) or "openai" in name:
        return "openai"
    if "gemini" in name or "palm" in name:
        return "google"
    if "qwen" in name or "deepseek" in name or "glm" in name:
        return "open-source-cn"
    return "unknown"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """一次模型调用的真实用量（来自 provider 的 usage 字段）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    external_requests: int = 1

    @property
    def billable_input_tokens(self) -> int:
        """缓存命中的部分不计价 —— DeepSeek 的 prompt cache 会单独报命中数。"""
        return max(0, self.input_tokens - self.cached_tokens)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["billable_input_tokens"] = self.billable_input_tokens
        return data

    @classmethod
    def from_api(cls, usage: dict | None) -> "ModelUsage":
        """解析 OpenAI 兼容的 usage 结构（含 DeepSeek 的缓存与推理 token 明细）。"""
        usage = usage or {}
        completion_details = usage.get("completion_tokens_details") or {}
        return cls(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            external_requests=1,
        )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model_family: str
    model_version: str
    prompt_hash: str
    usage: ModelUsage = ModelUsage()
    provider_request_id: str = ""
    finish_reason: str = ""


class ModelAdapter(Protocol):
    family: str
    version: str

    def complete(self, prompt: str) -> ModelResponse: ...

    def complete_chat(self, messages: list[dict]) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class IndependenceWaiver:
    """「worker 与 verifier 同家族」的显式豁免。

    存在的意义不是让护栏形同虚设，而是让**降级本身可见**：豁免必须被记录
    到证据链，任何人回放这次 run 都能看到「这次裁决没有跨家族独立性」。
    """

    reason: str
    worker_family: str
    verifier_family: str
    worker_model: str
    verifier_model: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelPair:
    worker: ModelAdapter
    verifier: ModelAdapter
    waiver: IndependenceWaiver | None = None

    def __iter__(self):
        """保持 `worker, verifier = build_models(...)` 的旧写法可用。"""
        return iter((self.worker, self.verifier))


class ScriptedModel:
    """按脚本返回预设输出，用于确定性演示与单元测试；不是真模型。

    script 用尽后重复最后一条 —— 这正是 `always_fail` 场景需要的性质。
    """

    def __init__(self, family: str, version: str, script: list[str]):
        if not script:
            raise ValueError("script 不能为空")
        self.family = family
        self.version = version
        self.script = list(script)
        self.calls = 0

    def complete(self, prompt: str) -> ModelResponse:
        return self.complete_chat([{"role": "user", "content": prompt}])

    def complete_chat(self, messages: list[dict]) -> ModelResponse:
        """多轮对话：工具循环（读文件 → 再决定）需要它。

        脚本模型按顺序吐预设回复；真实模型把 messages 原样发给 provider。
        """
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        return ModelResponse(
            text=self.script[idx],
            model_family=self.family,
            model_version=self.version,
            prompt_hash=sha256_text(prompt),
            usage=ModelUsage(),
        )


def _provider_slug(api_base: str) -> str:
    """从 API base 推出密钥文件名用的 provider 名。

    `api.deepseek.com` → `deepseek`（不是 `api`）；`api.openai.com` → `openai`。
    """
    host = api_base.split("//", 1)[-1].split("/", 1)[0].lower()
    labels = [x for x in host.split(".") if x]
    if not labels:
        return "provider"
    if labels[0] in ("api", "www", "openai", "gateway") and len(labels) >= 2:
        return labels[1]
    return labels[0]


def resolve_api_key(api_base: str, explicit: str | None = None) -> str:
    """按优先级找密钥：显式参数 → MW_API_KEY → MW_API_KEY_FILE → 默认文件。

    默认文件是 `~/.config/michelin/<provider>_api_key`（600 权限），不在仓库里。
    """
    if explicit:
        return explicit.strip()
    env = os.getenv("MW_API_KEY", "").strip()
    if env:
        return env
    path_env = os.getenv("MW_API_KEY_FILE", "").strip()
    candidates = [Path(path_env).expanduser()] if path_env else []
    candidates.append(Path.home() / ".config" / "michelin" / f"{_provider_slug(api_base)}_api_key")
    for candidate in candidates:
        try:
            if candidate.is_file():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    raise RuntimeError(
        "找不到模型密钥。请设置 MW_API_KEY，或把密钥写到 "
        f"~/.config/michelin/{_provider_slug(api_base)}_api_key（chmod 600）。"
    )


class OpenAICompatModel:
    """OpenAI 兼容 /chat/completions。仅依赖标准库。

    做了三件真实模型才会遇到的事：
    1. **用量如实记录**：解析 provider 的 usage（DeepSeek 还给缓存命中与推理 token），
       让成本熔断能按真实 token 生效，而不是按「调用次数」猜。
    2. **退避重试**：429 / 5xx / 网络抖动都重试，避免一次瞬时错误毁掉整条 run。
    3. **密钥不外泄**：任何错误信息都不含密钥。
    """

    RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        attempts: int = 3,
        max_tokens: int | None = None,
    ):
        self.version = model
        self.family = family_of(model)
        self.api_base = (api_base or os.getenv("MW_API_BASE", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = resolve_api_key(self.api_base, api_key)
        self.timeout = timeout or float(os.getenv("MW_API_TIMEOUT", "180"))
        self.attempts = max(1, attempts)
        self.max_tokens = max_tokens
        self.last_usage = ModelUsage()

    # ---------- 内部 ----------
    def _payload_messages(self, messages: list[dict]) -> bytes:
        body: dict = {
            "model": self.version,
            "messages": messages,
            "temperature": 0,
        }
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        return json.dumps(body).encode("utf-8")

    def _payload(self, prompt: str) -> bytes:
        return self._payload_messages([{"role": "user", "content": prompt}])

    @staticmethod
    def _safe_error(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - 读取失败不应掩盖原错误
            body = ""
        return body[:300]

    # ---------- 对外 ----------
    def complete(self, prompt: str) -> ModelResponse:
        return self.complete_chat([{"role": "user", "content": prompt}])

    def complete_chat(self, messages: list[dict]) -> ModelResponse:
        """多轮对话：工具循环需要它（读文件 → 再决定改什么）。"""
        data = self._payload_messages(messages)
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            req = urllib.request.Request(
                f"{self.api_base}/chat/completions",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "michelin-workbench",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in self.RETRY_STATUS and attempt < self.attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    f"模型调用失败 HTTP {exc.code}: {self._safe_error(exc)}"
                ) from None
            except (urllib.error.URLError, ssl.SSLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt < self.attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # 保留底层原因：只报 "URLError" 等于让人瞎猜。
                reason = getattr(exc, "reason", None) or str(exc)
                raise RuntimeError(
                    f"模型调用失败（网络）: {type(exc).__name__}: {reason}"
                ) from None
        else:  # pragma: no cover - 循环内要么 break 要么 raise
            raise RuntimeError(f"模型调用失败: {last_error}") from None

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = ModelUsage.from_api(payload.get("usage"))
        self.last_usage = usage
        return ModelResponse(
            text=text,
            model_family=self.family,
            model_version=self.version,
            prompt_hash=sha256_text("\n".join(str(m.get("content", "")) for m in messages)),
            usage=usage,
            provider_request_id=str(payload.get("id") or ""),
            finish_reason=str(choice.get("finish_reason") or ""),
        )


class MeteredModel:
    """把每次调用的真实用量交给外部计费函数（通常是预算账本）。

    用回调而不是直接依赖 runtime.budget，是为了让模型层保持零基础设施依赖；
    计费口径（input/output token、外部请求数）由调用方决定。
    """

    def __init__(self, inner: ModelAdapter, charge):
        self.inner = inner
        self.family = inner.family
        self.version = inner.version
        self._charge = charge
        # 累加器从 0 起算：ModelUsage() 的默认 external_requests=1 是「单次调用」
        # 的语义，累加器必须从零开始，否则多算一次。
        self.total_usage = ModelUsage(external_requests=0)

    def complete(self, prompt: str) -> ModelResponse:
        return self.complete_chat([{"role": "user", "content": prompt}])

    def complete_chat(self, messages: list[dict]) -> ModelResponse:
        """多轮也要计量：工具循环每一轮都是一次真实调用。"""
        response = self.inner.complete_chat(messages)
        usage = response.usage
        self.total_usage = ModelUsage(
            input_tokens=self.total_usage.input_tokens + usage.input_tokens,
            output_tokens=self.total_usage.output_tokens + usage.output_tokens,
            reasoning_tokens=self.total_usage.reasoning_tokens + usage.reasoning_tokens,
            cached_tokens=self.total_usage.cached_tokens + usage.cached_tokens,
            total_tokens=self.total_usage.total_tokens + usage.total_tokens,
            external_requests=self.total_usage.external_requests + usage.external_requests,
        )
        self._charge(usage)
        return response


def check_family_independence(
    worker: ModelAdapter,
    verifier: ModelAdapter,
    *,
    allow_same_family: bool | None = None,
) -> IndependenceWaiver | None:
    """护栏本体：worker 与 verifier 必须分属不同模型家族。

    不同家族 → 返回 None（正常）。
    同家族 → 默认抛错；只有显式放行才返回一份**豁免对象**，由调用方写进证据链。

    抽成独立函数是为了能脱离网络单测这条护栏（否则只能靠真连 provider 才能触发）。
    """
    if worker.family != verifier.family:
        return None
    allowed = (
        os.getenv("MW_ALLOW_SAME_MODEL_FAMILY", "").strip().lower() in ("1", "true", "yes")
        if allow_same_family is None else allow_same_family
    )
    if not allowed:
        raise RuntimeError(
            f"worker 与 verifier 模型家族相同 ({worker.family})，违反 pstack holdout 约束。\n"
            f"  worker={worker.version} / verifier={verifier.version}\n"
            "  若确实要用同一家的模型，请显式设置 MW_ALLOW_SAME_MODEL_FAMILY=1 —— \n"
            "  这份豁免会被写进证据链，任何人回放都能看到这次裁决没有跨家族独立性。"
        )
    return IndependenceWaiver(
        reason=os.getenv(
            "MW_INDEPENDENCE_WAIVER_REASON",
            "运营方显式声明接受同家族模型（家族级独立性已降级，结构性 holdout 仍在）",
        ),
        worker_family=worker.family,
        verifier_family=verifier.family,
        worker_model=worker.version,
        verifier_model=verifier.version,
    )


def build_models(
    *,
    backend: str | None = None,
    worker_script: list[str] | None = None,
    verifier_script: list[str] | None = None,
    allow_same_family: bool | None = None,
) -> ModelPair:
    """构造 (worker, verifier)，并保证二者模型家族不同（或带显式豁免）。

    `allow_same_family` 默认读环境变量 `MW_ALLOW_SAME_MODEL_FAMILY`。
    允许同家族时**必须**给出豁免对象，否则静默降级会让 holdout 保证消失得无影无踪。
    """
    backend = backend or os.getenv("MW_MODEL_BACKEND", "scripted")
    if backend == "scripted":
        worker: ModelAdapter = ScriptedModel(
            "anthropic", "claude-stub-p0", worker_script or ["ok"]
        )
        verifier: ModelAdapter = ScriptedModel(
            "openai", "gpt-stub-p0", verifier_script or ["PASS"]
        )
    elif backend == "openai_compat":
        worker = OpenAICompatModel(os.getenv("MW_WORKER_MODEL", "claude-sonnet-4-5"))
        verifier = OpenAICompatModel(os.getenv("MW_VERIFIER_MODEL", "gpt-5-mini"))
    elif backend == "deepseek":
        base = os.getenv("MW_API_BASE", "https://api.deepseek.com/v1")
        worker = OpenAICompatModel(
            os.getenv("MW_WORKER_MODEL", "deepseek-flash"), api_base=base
        )
        verifier = OpenAICompatModel(
            os.getenv("MW_VERIFIER_MODEL", "deepseek-v4-pro"), api_base=base
        )
    else:
        raise ValueError(f"未知模型后端: {backend}")

    waiver = check_family_independence(
        worker, verifier, allow_same_family=allow_same_family
    )
    return ModelPair(worker=worker, verifier=verifier, waiver=waiver)
