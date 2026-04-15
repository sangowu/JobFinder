"""统一 LLM 后端接口。

支持的 provider：
  云端 (原生 SDK)  : claude (Anthropic), gemini (Google)
  云端 (OpenAI 兼容): openai, xai, mistral, meta, huggingface,
                      qwen, glm, kimi, deepseek, minimax,
                      doubao, siliconflow, openrouter
  本地             : ollama (llama.cpp OpenAI 兼容接口), local (任意 OpenAI 兼容接口)

所有模型默认关闭 thinking/reasoning 模式，使用 text-only 端点。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import requests
from pydantic import BaseModel

# ─── Provider 类型 ────────────────────────────────────────────────────────────

Provider = Literal[
    # 原生 SDK
    "claude", "gemini",
    # OpenAI 兼容 — 国际
    "openai", "xai", "mistral",
    # OpenAI 兼容 — 中国
    "qwen", "glm", "kimi", "deepseek", "minimax",
    # 本地
    "ollama", "local",
]

# ─── OpenAI 兼容 provider 注册表 ──────────────────────────────────────────────
# base_url=None 表示从环境变量动态读取

_COMPAT_PROVIDERS: dict[str, dict[str, str | None]] = {
    # 国际
    "openai":       {"base_url": "https://api.openai.com/v1",                          "key_env": "OPENAI_API_KEY"},
    "xai":          {"base_url": "https://api.x.ai/v1",                                "key_env": "XAI_API_KEY"},
    "mistral":      {"base_url": "https://api.mistral.ai/v1",                           "key_env": "MISTRAL_API_KEY"},
    # 中国
    "qwen":         {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",  "key_env": "DASHSCOPE_API_KEY"},
    "glm":          {"base_url": "https://open.bigmodel.cn/api/paas/v4",               "key_env": "ZHIPUAI_API_KEY"},
    "kimi":         {"base_url": "https://api.moonshot.cn/v1",                         "key_env": "MOONSHOT_API_KEY"},
    "deepseek":     {"base_url": "https://api.deepseek.com/v1",                        "key_env": "DEEPSEEK_API_KEY"},
    "minimax":      {"base_url": "https://api.minimax.io/v1",                          "key_env": "MINIMAX_API_KEY"},
    # 本地 OpenAI 兼容
    "ollama":       {"base_url": None, "base_url_env": "LLAMACPP_BASE_URL", "base_url_default": "http://localhost:8080/v1", "key_env": "LLAMACPP_API_KEY"},
    "local":        {"base_url": None, "base_url_env": "LOCAL_LLM_BASE_URL", "base_url_default": "http://localhost:1234/v1", "key_env": "LOCAL_LLM_API_KEY"},
}

# ─── 默认模型 ─────────────────────────────────────────────────────────────────

DEFAULT_MODELS: dict[str, str] = {
    # 原生 SDK
    "claude":       "claude-haiku-4.5",
    "gemini":       "gemini-3.1-flash-lite-preview",
    # OpenAI 兼容 — 国际
    "openai":       "gpt-5.4-mini",
    "xai":          "grok-4",
    "mistral":      "mistral-small-2603",
    # OpenAI 兼容 — 中国
    "qwen":         "qwen3.5-flash-02-23",
    "glm":          "glm-4.7-flash",
    "kimi":         "kimi-k2",
    "deepseek":     "deepseek-chat-v3.1",
    "minimax":      "minimax-m2",
    # 本地
    "ollama":       "llama-3.2-3b-instruct",
    "local":        "local-model",
}

# ─── 可选模型列表（供 Web UI / CLI 展示）──────────────────────────────────────

AVAILABLE_MODELS: dict[str, list[str]] = {
    "claude": [],   # 本地动态获取 / 无 API Key
    "gemini": [],   # 本地动态获取 / 无 API Key
    "openai": [],   # 本地动态获取 / 无 API Key
    "xai": [],   # 本地动态获取 / 无 API Key
    "mistral": [],   # 本地动态获取 / 无 API Key
    "qwen": [],   # 本地动态获取 / 无 API Key
    "glm": [],   # 本地动态获取 / 无 API Key
    "kimi": [],   # 本地动态获取 / 无 API Key
    "deepseek": [],   # 本地动态获取 / 无 API Key
    "minimax": [],   # 本地动态获取 / 无 API Key
    "ollama": [],   # 本地动态获取 / 无 API Key
    "local": [],   # 本地动态获取 / 无 API Key
}

# ─── LLMConfig ────────────────────────────────────────────────────────────────


@dataclass
class LLMConfig:
    """统一的 LLM 配置，贯穿所有 skill 调用，避免 provider/model 重复传参。"""
    provider: str
    model: str

    @classmethod
    def from_defaults(cls, provider: str) -> "LLMConfig":
        return cls(provider=provider, model=DEFAULT_MODELS.get(provider, ""))


# ─── 统一响应格式 ─────────────────────────────────────────────────────────────


@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class ToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class NormalizedResponse:
    stop_reason: str  # "end_turn" | "tool_use"
    content: list[TextBlock | ToolUseBlock]


# ─── 公共入口 ─────────────────────────────────────────────────────────────────


def complete_structured(
    prompt: str,
    response_schema: type[BaseModel],
    provider: str = "gemini",
    model: str | None = None,
    system: str = "你是一个专业的信息提取助手，严格按照指定格式返回 JSON。",
    _step: str = "",
) -> BaseModel:
    """调用 LLM，返回符合 response_schema 的 Pydantic 对象。"""
    import time
    from jobfinder.telemetry import telemetry

    m = model or DEFAULT_MODELS.get(provider, "")
    t0 = time.monotonic()

    if provider == "claude":
        result, in_tok, out_tok = _claude_structured(prompt, response_schema, system, m)
    elif provider == "gemini":
        result, in_tok, out_tok = _gemini_structured(prompt, response_schema, system, m)
    elif provider in _COMPAT_PROVIDERS:
        result, in_tok, out_tok = _compat_structured(prompt, response_schema, system, m, provider)
    else:
        raise ValueError(f"不支持的 provider：{provider}")

    if _step:
        telemetry.record_llm(
            step=_step, provider=provider, model=m,
            input_tokens=in_tok, output_tokens=out_tok,
            elapsed=time.monotonic() - t0,
        )
    return result


def complete_with_tools(
    messages: list[dict],
    tools: list[dict],
    system: str,
    provider: str = "claude",
    model: str | None = None,
) -> NormalizedResponse:
    """支持 tool use 的对话调用，返回统一格式 NormalizedResponse。"""
    m = model or DEFAULT_MODELS.get(provider, "")
    if provider == "claude":
        return _claude_tool_call(messages, tools, system, m)
    elif provider == "gemini":
        return _gemini_tool_call(messages, tools, system, m)
    elif provider in _COMPAT_PROVIDERS:
        return _compat_tool_call(messages, tools, system, m, provider)
    raise ValueError(f"不支持的 provider：{provider}")


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON，处理 markdown 代码块等包装。"""
    text = text.strip()
    # 去除 ```json ... ``` 包装
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()
    # 找到第一个 { 开始
    start = text.find("{")
    if start > 0:
        text = text[start:]
    return json.loads(text)


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _to_gemini_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in tools
    ]


# ─── Claude (Anthropic) ───────────────────────────────────────────────────────


def _get_anthropic():
    from anthropic import Anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY 未设置")
    return Anthropic(api_key=key)


def _claude_structured(prompt, schema, system, model) -> tuple[BaseModel, int, int]:
    client = _get_anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        tools=[{"name": "output", "description": "结构化输出",
                "input_schema": schema.model_json_schema()}],
        tool_choice={"type": "tool", "name": "output"},
        messages=[{"role": "user", "content": prompt}],
    )
    in_tok = response.usage.input_tokens
    out_tok = response.usage.output_tokens
    for block in response.content:
        if block.type == "tool_use":
            return schema.model_validate(block.input), in_tok, out_tok
    raise RuntimeError("Claude 未返回结构化输出")


def _to_claude_messages(messages: list[dict]) -> list[dict]:
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, list):
            claude_blocks = []
            for block in content:
                if isinstance(block, TextBlock):
                    claude_blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    claude_blocks.append({"type": "tool_use", "id": block.id,
                                          "name": block.name, "input": block.input})
                elif isinstance(block, dict) and block.get("type") == "tool_result":
                    claude_blocks.append({"type": "tool_result",
                                          "tool_use_id": block["tool_use_id"],
                                          "content": block["content"]})
                else:
                    claude_blocks.append(block)
            result.append({"role": role, "content": claude_blocks})
        else:
            result.append({"role": role, "content": content})
    return result


def _claude_tool_call(messages, tools, system, model) -> NormalizedResponse:
    client = _get_anthropic()
    response = client.messages.create(
        model=model, max_tokens=4096, system=system, tools=tools,
        messages=_to_claude_messages(messages),
    )
    content = []
    for block in response.content:
        if block.type == "text":
            content.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            content.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))
    stop = "tool_use" if response.stop_reason == "tool_use" else "end_turn"
    return NormalizedResponse(stop_reason=stop, content=content)


# ─── Gemini (Google) ──────────────────────────────────────────────────────────


def _get_gemini_client():
    from google import genai
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise EnvironmentError("GEMINI_API_KEY 未设置")
    return genai.Client(api_key=key)


def _gemini_structured(prompt, schema, system, model) -> tuple[BaseModel, int, int]:
    from google.genai import types
    client = _get_gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema.model_json_schema(),
        ),
    )
    meta = response.usage_metadata
    in_tok = meta.prompt_token_count if meta else 0
    out_tok = meta.candidates_token_count if meta else 0
    return schema.model_validate(json.loads(response.text)), in_tok, out_tok


def _to_gemini_contents(messages: list[dict]) -> list:
    from google.genai import types
    result = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        content = msg["content"]
        parts = []
        if isinstance(content, list):
            for p in content:
                if isinstance(p, ToolUseBlock):
                    parts.append(types.Part.from_function_call(name=p.name, args=p.input))
                elif isinstance(p, TextBlock) and p.text:
                    parts.append(types.Part.from_text(text=p.text))
                elif isinstance(p, dict) and p.get("type") == "tool_result":
                    parts.append(types.Part.from_function_response(
                        name=p["tool_use_id"],
                        response={"result": p.get("content", "")},
                    ))
                elif isinstance(p, dict) and "text" in p:
                    parts.append(types.Part.from_text(text=p["text"]))
                elif isinstance(p, dict):
                    parts.append(types.Part.from_text(text=json.dumps(p, ensure_ascii=False)))
        elif isinstance(content, str):
            parts = [types.Part.from_text(text=content)]
        if parts:
            result.append(types.Content(role=role, parts=parts))
    return result


def _gemini_tool_call(messages, tools, system, model) -> NormalizedResponse:
    from google.genai import types
    client = _get_gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=_to_gemini_contents(messages),
        config=types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=_to_gemini_tools(tools))],
        ),
    )
    if not response.candidates:
        return NormalizedResponse(stop_reason="end_turn", content=[
            TextBlock(text="[Gemini 未返回候选结果]")
        ])
    candidate = response.candidates[0]
    if not candidate.content or not candidate.content.parts:
        return NormalizedResponse(stop_reason="end_turn", content=[])
    content: list[TextBlock | ToolUseBlock] = []
    has_tool_call = False
    for part in candidate.content.parts:
        if part.text:
            content.append(TextBlock(text=part.text))
        elif part.function_call:
            has_tool_call = True
            fc = part.function_call
            content.append(ToolUseBlock(
                id=fc.name, name=fc.name,
                input=dict(fc.args) if fc.args else {},
            ))
    return NormalizedResponse(
        stop_reason="tool_use" if has_tool_call else "end_turn",
        content=content,
    )


# ─── OpenAI 兼容（通用） ──────────────────────────────────────────────────────


def _get_compat_client(provider: str):
    """根据 provider 构造 OpenAI 兼容客户端。"""
    from openai import OpenAI
    cfg = _COMPAT_PROVIDERS[provider]
    base_url = (
        cfg["base_url"]
        or os.getenv(cfg.get("base_url_env", ""), "")
        or cfg.get("base_url_default", "http://localhost:1234/v1")
    )
    key = os.getenv(cfg["key_env"] or "") or "not-required"
    return OpenAI(api_key=key, base_url=base_url)


def _compat_structured(prompt, schema, system, model, provider) -> tuple[BaseModel, int, int]:
    """OpenAI 兼容结构化输出：schema 注入 system prompt + json_object 格式。"""
    client = _get_compat_client(provider)
    schema_str = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    full_system = (
        f"{system}\n\n"
        f"必须严格按照以下 JSON Schema 返回，不含任何其他文字或代码块：\n{schema_str}"
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": full_system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2048,
    }

    # 尝试 json_object 格式（部分 provider 支持）
    try:
        response = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"}
        )
    except Exception:
        response = client.chat.completions.create(**kwargs)

    in_tok  = response.usage.prompt_tokens     if response.usage else 0
    out_tok = response.usage.completion_tokens if response.usage else 0
    text    = response.choices[0].message.content or ""
    return schema.model_validate(_extract_json(text)), in_tok, out_tok


def _to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    result = [{"role": "system", "content": system}]
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if role == "assistant" and isinstance(content, list):
            tool_calls, text = [], None
            for block in content:
                if isinstance(block, ToolUseBlock):
                    tool_calls.append({
                        "id": block.id, "type": "function",
                        "function": {"name": block.name,
                                     "arguments": json.dumps(block.input)},
                    })
                elif isinstance(block, TextBlock) and block.text:
                    text = block.text
            openai_msg: dict = {"role": "assistant"}
            if text:
                openai_msg["content"] = text
            if tool_calls:
                openai_msg["tool_calls"] = tool_calls
            result.append(openai_msg)
        elif role == "user" and isinstance(content, list):
            tool_results = [p for p in content
                            if isinstance(p, dict) and p.get("type") == "tool_result"]
            if tool_results:
                for tr in tool_results:
                    result.append({"role": "tool",
                                   "tool_call_id": tr["tool_use_id"],
                                   "content": tr["content"]})
            else:
                result.append({"role": "user", "content": str(content)})
        else:
            result.append({"role": role,
                           "content": content if isinstance(content, str) else str(content)})
    return result


def _compat_tool_call(messages, tools, system, model, provider) -> NormalizedResponse:
    client = _get_compat_client(provider)
    response = client.chat.completions.create(
        model=model,
        messages=_to_openai_messages(messages, system),
        tools=_to_openai_tools(tools),
    )
    msg = response.choices[0].message
    content: list[TextBlock | ToolUseBlock] = []
    if msg.content:
        content.append(TextBlock(text=msg.content))
    if msg.tool_calls:
        for tc in msg.tool_calls:
            content.append(ToolUseBlock(
                id=tc.id, name=tc.function.name,
                input=json.loads(tc.function.arguments),
            ))
        return NormalizedResponse(stop_reason="tool_use", content=content)
    return NormalizedResponse(stop_reason="end_turn", content=content)


# ─── Ollama（原生接口） ───────────────────────────────────────────────────────


# ─── 动态模型列表 ─────────────────────────────────────────────────────────────


def _llamacpp_base_url() -> str:
    return os.getenv("LLAMACPP_BASE_URL", "http://localhost:8080/v1")


def check_llamacpp_connection() -> bool:
    """检测 llama.cpp 服务是否可用（GET /v1/models）。"""
    try:
        return requests.get(f"{_llamacpp_base_url()}/models", timeout=5).status_code == 200
    except Exception:
        return False


def get_llamacpp_models() -> list[str]:
    """从 llama.cpp OpenAI 兼容接口获取已加载的模型列表。"""
    try:
        resp = requests.get(f"{_llamacpp_base_url()}/models", timeout=5)
        data = resp.json()
        models = data.get("data", data) if isinstance(data, dict) else data
        return [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")]
    except Exception:
        return []


# 向后兼容别名
check_ollama_connection = check_llamacpp_connection
get_ollama_models = get_llamacpp_models


def get_local_models() -> list[str]:
    """尝试从本地 OpenAI 兼容服务获取模型列表。"""
    base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    try:
        from openai import OpenAI
        client = OpenAI(api_key="not-required", base_url=base_url)
        return [m.id for m in client.models.list()]
    except Exception:
        return []


def get_gemini_models() -> list[str]:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return AVAILABLE_MODELS["gemini"]
    try:
        from google import genai
        client = genai.Client(api_key=key)
        models = [
            m.name.removeprefix("models/")
            for m in client.models.list()
            if "gemini" in m.name and "generateContent" in (m.supported_actions or [])
        ]
        return sorted(models, reverse=True) or AVAILABLE_MODELS["gemini"]
    except Exception:
        return AVAILABLE_MODELS["gemini"]


def get_openai_models() -> list[str]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return AVAILABLE_MODELS["openai"]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        gpt = sorted(
            [m.id for m in client.models.list()
             if m.id.startswith("gpt") and "instruct" not in m.id],
            reverse=True,
        )
        return gpt or AVAILABLE_MODELS["openai"]
    except Exception:
        return AVAILABLE_MODELS["openai"]


def check_provider_connection(provider: str, model: str | None = None) -> tuple[bool, str]:
    """
    检测指定 provider 是否可用。
    返回 (ok: bool, message: str)。
    """
    from pydantic import BaseModel as BM

    class _Ping(BM):
        ok: bool

    m = model or DEFAULT_MODELS.get(provider, "")
    try:
        complete_structured(
            prompt='返回 {"ok": true}',
            response_schema=_Ping,
            provider=provider,
            model=m,
            system="你是一个测试助手，只返回 JSON。",
            _step="",
        )
        return True, f"{provider} / {m} connected"
    except EnvironmentError as e:
        return False, f"API key not set: {e}"
    except Exception as e:
        return False, f"Connection failed: {e}"
