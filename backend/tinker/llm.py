"""LLM 客户端：OpenAI 兼容接口（默认 DeepSeek），结果落盘缓存。

配置走环境变量（不落入代码与仓库）：
- TINKER_LLM_API_KEY   必填，未配置时标注功能明确报错
- TINKER_LLM_BASE_URL  默认 https://api.deepseek.com
- TINKER_LLM_MODEL     默认 deepseek-chat

缓存：key = sha256(model + prompt)，value 为模型返回的文本。
重复请求（同一模型同一 prompt）直接读缓存，不调用 API——这是 spec 的硬要求。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

BASE_URL = os.environ.get("TINKER_LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("TINKER_LLM_MODEL", "deepseek-chat")

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "llm-cache"


class LLMError(Exception):
    """LLM 调用失败，message 面向用户。"""


def configured() -> bool:
    return bool(os.environ.get("TINKER_LLM_API_KEY"))


def chat(prompt: str) -> str:
    """发送单轮对话，返回模型文本；命中缓存时不发请求。"""
    key = hashlib.sha256(f"{MODEL}\n{prompt}".encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))["response"]

    api_key = os.environ.get("TINKER_LLM_API_KEY")
    if not api_key:
        raise LLMError("没有配置 LLM API key（设置环境变量 TINKER_LLM_API_KEY）")
    try:
        res = httpx.post(
            f"{BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=120,
        )
        res.raise_for_status()
        text = res.json()["choices"][0]["message"]["content"]
    except httpx.HTTPError as e:
        raise LLMError(f"LLM 请求失败：{e}") from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise LLMError(f"LLM 返回格式异常：{e}") from e

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"prompt": prompt, "response": text}, ensure_ascii=False), encoding="utf-8")
    return text
