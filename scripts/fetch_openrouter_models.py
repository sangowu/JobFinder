"""
从各 provider 官方 API 获取可用模型列表，输出可直接粘贴到 llm_backend.py 的字典。

用法：
    uv run python scripts/fetch_openrouter_models.py           # 预览
    uv run python scripts/fetch_openrouter_models.py --patch   # 直接写入 llm_backend.py
    uv run python scripts/fetch_openrouter_models.py --top 8   # 每个 provider 最多 8 个

需要在 .env 中配置对应的 API Key，未配置的 provider 自动跳过。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 15

# ── 排除规则（适用于所有 provider）────────────────────────────────────────────

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


def _is_chat_model(model_id: str, name: str = "") -> bool:
    if EXCLUDE_ID.search(model_id):
        return False
    if name and EXCLUDE_NAME.search(name):
        return False
    return True


# ── 各 provider 抓取函数 ───────────────────────────────────────────────────────

def _fetch_gemini(top_n: int) -> list[str]:
    """Google Gemini：官方 REST endpoint，按 name 字母倒序取 top_n。"""
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key, "pageSize": 200},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    models = resp.json().get("models", [])
    ids = []
    for m in models:
        raw = m.get("name", "")               # "models/gemini-2.5-flash"
        mid = raw.removeprefix("models/")
        name = m.get("displayName", "")
        # 只保留支持 generateContent 的模型
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        if not _is_chat_model(mid, name):
            continue
        ids.append(mid)
    # 按名称字母倒序（新版本号通常更大）
    ids.sort(reverse=True)
    return ids[:top_n]


def _fetch_claude(top_n: int) -> list[str]:
    """Anthropic Claude：官方 /v1/models 端点。"""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    models = resp.json().get("data", [])
    ids = [m["id"] for m in models if _is_chat_model(m.get("id", ""), m.get("display_name", ""))]
    ids.sort(reverse=True)
    return ids[:top_n]


def _fetch_compat(base_url: str, api_key: str, top_n: int) -> list[str]:
    """OpenAI 兼容端点：GET /models。"""
    resp = requests.get(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # 兼容 {"data": [...]} 和直接返回列表两种格式
    models = data.get("data", data) if isinstance(data, dict) else data
    ids = []
    for m in models:
        mid = m.get("id", "") if isinstance(m, dict) else str(m)
        if _is_chat_model(mid):
            ids.append(mid)
    ids.sort(reverse=True)
    return ids[:top_n]


# ── provider 配置表 ────────────────────────────────────────────────────────────

# (fetch_fn, key_env_var)  None fetch_fn = 使用 _fetch_compat
PROVIDERS: dict[str, tuple] = {
    "claude":   (_fetch_claude,  None),
    "gemini":   (_fetch_gemini,  None),
    "openai":   (None, "OPENAI_API_KEY",   "https://api.openai.com/v1"),
    "xai":      (None, "XAI_API_KEY",      "https://api.x.ai/v1"),
    "mistral":  (None, "MISTRAL_API_KEY",  "https://api.mistral.ai/v1"),
    "qwen":     (None, "DASHSCOPE_API_KEY","https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":      (None, "ZHIPUAI_API_KEY",  "https://open.bigmodel.cn/api/paas/v4"),
    "kimi":     (None, "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1"),
    "deepseek": (None, "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    "minimax":  (None, "MINIMAX_API_KEY",  "https://api.minimax.io/v1"),
    "ollama":   None,   # 本地，跳过
    "local":    None,   # 本地，跳过
}


def fetch_all(top_n: int) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for provider, cfg in PROVIDERS.items():
        if cfg is None:
            result[provider] = []
            continue

        fetch_fn, *rest = cfg

        try:
            if fetch_fn is not None:
                # 原生 SDK provider（claude / gemini）
                ids = fetch_fn(top_n)
                status = f"{len(ids)} 个" if ids else "跳过（无 API Key）"
            else:
                # OpenAI 兼容 provider
                key_env, base_url = rest
                key = os.getenv(key_env)
                if not key:
                    result[provider] = []
                    print(f"  {provider:<12s}  跳过（{key_env} 未配置）")
                    continue
                ids = _fetch_compat(base_url, key, top_n)
                status = f"{len(ids)} 个"
        except Exception as e:
            ids = []
            status = f"失败：{e}"

        result[provider] = ids
        print(f"  {provider:<12s}  {status}  {ids[:3]}{'…' if len(ids) > 3 else ''}")

    return result


# ── 输出 / patch ───────────────────────────────────────────────────────────────

def print_result(available: dict[str, list[str]]) -> None:
    print("\n# ── 结果（可粘贴至 llm_backend.py AVAILABLE_MODELS）────────────────\n")
    print("AVAILABLE_MODELS: dict[str, list[str]] = {")
    for provider, ids in available.items():
        if not ids:
            print(f'    "{provider}": [],   # 本地动态获取 / 无 API Key')
        else:
            ids_repr = ",\n        ".join(f'"{i}"' for i in ids)
            print(f'    "{provider}": [\n        {ids_repr},\n    ],')
    print("}")


def patch_llm_backend(available: dict[str, list[str]]) -> None:
    target = Path(__file__).parent.parent / "jobfinder" / "llm_backend.py"
    if not target.exists():
        print(f"[ERROR] 找不到文件：{target}", file=sys.stderr)
        sys.exit(1)

    lines = ["AVAILABLE_MODELS: dict[str, list[str]] = {\n"]
    for provider, ids in available.items():
        if not ids:
            lines.append(f'    "{provider}": [],   # 本地动态获取 / 无 API Key\n')
        else:
            lines.append(f'    "{provider}": [\n')
            for mid in ids:
                lines.append(f'        "{mid}",\n')
            lines.append("    ],\n")
    lines.append("}\n")
    new_block = "".join(lines)

    src = target.read_text(encoding="utf-8")
    pattern = re.compile(
        r"AVAILABLE_MODELS:\s*dict\[str,\s*list\[str\]\]\s*=\s*\{.*?\n\}",
        re.DOTALL,
    )
    if not pattern.search(src):
        print("[ERROR] 未找到 AVAILABLE_MODELS 块", file=sys.stderr)
        sys.exit(1)

    target.write_text(pattern.sub(new_block.rstrip("\n"), src), encoding="utf-8")
    print(f"\n[OK] 已写入 {target}")


# ── 入口 ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="从各 provider 官方 API 获取模型列表")
    parser.add_argument("--top",   type=int, default=6,  help="每个 provider 保留最多 N 个（默认 6）")
    parser.add_argument("--patch", action="store_true",  help="直接写入 llm_backend.py")
    args = parser.parse_args()

    print(f"查询各 provider 官方模型列表（top={args.top}）…\n")
    available = fetch_all(args.top)

    if args.patch:
        patch_llm_backend(available)
    else:
        print_result(available)
        print("\n提示：加 --patch 参数可直接写入 llm_backend.py")


if __name__ == "__main__":
    main()
