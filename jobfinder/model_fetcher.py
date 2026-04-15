"""
从各 provider 官方 API 获取可用模型列表。
供 server.py 的 /api/models/refresh 端点调用。
"""
from __future__ import annotations

import os
import re

import requests

from jobfinder.logger import get_logger

logger = get_logger(__name__)

TIMEOUT = 12

EXCLUDE_ID = re.compile(
    r"(\bgemma-|lyria-|robotics|nano-banana"
    r"|-tts\b|-image\b|-latest$|-computer-use"
    r"|-base|-pretrain|-codex\b|safeguard"
    r"|-moderat|whisper|-embed|-search|deep-research)",
    re.IGNORECASE,
)
EXCLUDE_NAME = re.compile(
    r"(image|text-to-image|embedding|moderation|audio|music|banana|robotics|TTS|computer use)",
    re.IGNORECASE,
)


def _ok(model_id: str, name: str = "") -> bool:
    if EXCLUDE_ID.search(model_id):
        return False
    if name and EXCLUDE_NAME.search(name):
        return False
    return True


def _fetch_gemini(top_n: int) -> list[str]:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key, "pageSize": 200},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    ids = []
    for m in resp.json().get("models", []):
        mid = m.get("name", "").removeprefix("models/")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if _ok(mid, m.get("displayName", "")):
            ids.append(mid)
    ids.sort(reverse=True)
    return ids[:top_n]


def _fetch_claude(top_n: int) -> list[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    ids = [
        m["id"] for m in resp.json().get("data", [])
        if _ok(m.get("id", ""), m.get("display_name", ""))
    ]
    ids.sort(reverse=True)
    return ids[:top_n]


def _fetch_compat(base_url: str, api_key: str, top_n: int) -> list[str]:
    resp = requests.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    models = data.get("data", data) if isinstance(data, dict) else data
    ids = [
        m.get("id", "") for m in models
        if isinstance(m, dict) and _ok(m.get("id", ""))
    ]
    ids.sort(reverse=True)
    return ids[:top_n]


def _fetch_llamacpp(top_n: int) -> list[str]:
    """从 llama.cpp OpenAI 兼容接口获取模型列表（无需 API Key）。"""
    base_url = os.getenv("LLAMACPP_BASE_URL", "http://localhost:8080/v1")
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", data) if isinstance(data, dict) else data
        return [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")][:top_n]
    except Exception:
        return []


# provider → (fetch_fn 或 None, key_env, base_url)
_PROVIDER_CFG: dict[str, tuple] = {
    "claude":   (_fetch_claude,   None,                  None),
    "gemini":   (_fetch_gemini,   None,                  None),
    "openai":   (None, "OPENAI_API_KEY",   "https://api.openai.com/v1"),
    "xai":      (None, "XAI_API_KEY",      "https://api.x.ai/v1"),
    "mistral":  (None, "MISTRAL_API_KEY",  "https://api.mistral.ai/v1"),
    "qwen":     (None, "DASHSCOPE_API_KEY","https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":      (None, "ZHIPUAI_API_KEY",  "https://open.bigmodel.cn/api/paas/v4"),
    "kimi":     (None, "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"),
    "deepseek": (None, "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    "minimax":  (None, "MINIMAX_API_KEY",  "https://api.minimax.io/v1"),
    "ollama":   (_fetch_llamacpp, None,                  None),
}


def fetch_all(top_n: int = 6) -> dict[str, list[str]]:
    """
    遍历所有 provider，用已配置的 API Key 获取模型列表。
    未配置 Key 的 provider 返回空列表（不报错）。
    """
    result: dict[str, list[str]] = {}
    for provider, cfg in _PROVIDER_CFG.items():
        fetch_fn, key_env, base_url = cfg
        try:
            if fetch_fn is not None:
                ids = fetch_fn(top_n)
            else:
                key = os.getenv(key_env)
                if not key:
                    result[provider] = []
                    continue
                ids = _fetch_compat(base_url, key, top_n)
            result[provider] = ids
            logger.debug("model_fetcher [%s] → %d 个", provider, len(ids))
        except Exception as e:
            logger.warning("model_fetcher [%s] 失败：%s", provider, e)
            result[provider] = []
    return result
