from __future__ import annotations

import os
import re
from pathlib import Path

from jobradar.llm_registry import DEFAULT_MODELS, Provider, _COMPAT_PROVIDERS

PROVIDER_KEY_MAP: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    **{provider: config.get("key_env") or "" for provider, config in _COMPAT_PROVIDERS.items()},
}


def save_env_key(key: str, value: str, env_path: Path | None = None) -> None:
    target = env_path or (Path(os.getcwd()) / ".env")
    line = f"{key}={value}"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        if re.search(rf"^{re.escape(key)}=", content, re.MULTILINE):
            content = re.sub(rf"^{re.escape(key)}=.*$", line, content, flags=re.MULTILINE)
            target.write_text(content, encoding="utf-8")
            return
    with target.open("a", encoding="utf-8") as file_obj:
        file_obj.write(f"\n{line}\n")


def get_saved_defaults() -> tuple[str, str]:
    provider = os.getenv("DEFAULT_PROVIDER", "claude")
    model = os.getenv("DEFAULT_MODEL", DEFAULT_MODELS.get(provider, ""))
    return provider, model


def get_effective_model(provider: str, model: str | None = None, fallback_model: str | None = None) -> str:
    if model:
        return model
    if fallback_model:
        return fallback_model
    return DEFAULT_MODELS.get(provider, "")
