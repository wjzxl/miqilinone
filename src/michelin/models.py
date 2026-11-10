"""模型适配层：把「脑」与「脊」分开，并强制记录 prompt_hash / model_version。

P0 默认用 ScriptedModel（零依赖、确定性、今天就能跑通全链路）。
设置 MW_MODEL_BACKEND=openai_compat 后走 OpenAI 兼容 HTTP（标准库 urllib，无需 pip），
family 由模型名推断，worker 与 verifier 必须分属不同家族。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
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
class ModelResponse:
    text: str
    model_family: str
    model_version: str
    prompt_hash: str


class ModelAdapter(Protocol):
    family: str
    version: str

    def complete(self, prompt: str) -> ModelResponse: ...


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
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return ModelResponse(
            text=self.script[idx],
            model_family=self.family,
            model_version=self.version,
            prompt_hash=sha256_text(prompt),
        )


class OpenAICompatModel:
    """OpenAI 兼容 /chat/completions。仅依赖标准库。"""

    def __init__(self, model: str, *, api_base: str | None = None, api_key: str | None = None):
        self.version = model
        self.family = family_of(model)
        self.api_base = (api_base or os.getenv("MW_API_BASE", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("MW_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("MW_API_KEY 未设置；真实模型后端需要密钥")

    def complete(self, prompt: str) -> ModelResponse:
        body = json.dumps({
            "model": self.version,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return ModelResponse(
            text=text,
            model_family=self.family,
            model_version=self.version,
            prompt_hash=sha256_text(prompt),
        )


def build_models(
    *,
    backend: str | None = None,
    worker_script: list[str] | None = None,
    verifier_script: list[str] | None = None,
) -> tuple[ModelAdapter, ModelAdapter]:
    """返回 (worker_model, verifier_model)，并保证二者模型家族不同。"""
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
    else:
        raise ValueError(f"未知模型后端: {backend}")
    if worker.family == verifier.family:
        raise RuntimeError(
            f"worker 与 verifier 模型家族相同 ({worker.family})，违反 pstack holdout 约束"
        )
    return worker, verifier
