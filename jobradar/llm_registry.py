from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Provider = Literal[
    "claude",
    "gemini",
    "openai",
    "xai",
    "mistral",
    "qwen",
    "glm",
    "kimi",
    "deepseek",
    "minimax",
    "ollama",
    "local",
]

_COMPAT_PROVIDERS: dict[str, dict[str, str | None]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
    "xai": {"base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "key_env": "ZHIPUAI_API_KEY"},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "key_env": "MOONSHOT_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY"},
    "minimax": {"base_url": "https://api.minimax.io/v1", "key_env": "MINIMAX_API_KEY"},
    "ollama": {
        "base_url": None,
        "base_url_env": "LLAMACPP_BASE_URL",
        "base_url_default": "http://localhost:8080/v1",
        "key_env": "LLAMACPP_API_KEY",
    },
    "local": {
        "base_url": None,
        "base_url_env": "LOCAL_LLM_BASE_URL",
        "base_url_default": "http://localhost:1234/v1",
        "key_env": "LOCAL_LLM_API_KEY",
    },
}

DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-haiku-4.5",
    "gemini": "gemini-3.5-flash-lite",
    "openai": "gpt-5.4-mini",
    "xai": "grok-4",
    "mistral": "mistral-small-2603",
    "qwen": "qwen3.5-flash-02-23",
    "glm": "glm-4.7-flash",
    "kimi": "kimi-k2",
    "deepseek": "deepseek-chat-v3.1",
    "minimax": "minimax-m2",
    "ollama": "llama-3.2-3b-instruct",
    "local": "local-model",
}

AVAILABLE_MODELS: dict[str, list[str]] = {
    "claude": [],
    "gemini": [],
    "openai": [],
    "xai": [],
    "mistral": [],
    "qwen": [],
    "glm": [],
    "kimi": [],
    "deepseek": [],
    "minimax": [],
    "ollama": [],
    "local": [],
}


@dataclass
class LLMConfig:
    provider: str
    model: str

    @classmethod
    def from_defaults(cls, provider: str) -> "LLMConfig":
        return cls(provider=provider, model=DEFAULT_MODELS.get(provider, ""))
