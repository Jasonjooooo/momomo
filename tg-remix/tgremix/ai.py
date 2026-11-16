"""调用 OpenAI 兼容接口（默认 DeepSeek）——AI 解说和机器人筛选共用"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .runtime import ssl_context


class AIError(Exception):
    pass


def chat(acfg: dict, prompt: str, max_tokens: int = 400, temperature: float = 0.7, timeout: int = 60) -> str:
    key = str(acfg.get("api_key") or "").strip()
    if not key:
        raise AIError("没有填写 AI 接口的 API Key（在「效果 → AI 解说配音」里填写）")
    base = str(acfg.get("api_base") or "https://api.deepseek.com").rstrip("/")
    body = json.dumps({
        "model": acfg.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:200]
        if e.code == 401:
            raise AIError("API Key 不对或已失效") from e
        if e.code == 402:
            raise AIError("AI 接口余额不足，请充值") from e
        raise AIError(f"AI 接口返回错误 {e.code}：{detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIError(f"连不上 AI 接口（{base}）：{e}") from e
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise AIError(f"AI 接口返回格式不对：{str(data)[:200]}") from e


def chat_json(acfg: dict, prompt: str, **kw) -> dict:
    """要求 AI 只回 JSON，并从回答里把 JSON 抠出来"""
    text = chat(acfg, prompt, **kw)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError(f"AI 没有按格式回答：{text[:120]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AIError(f"AI 回答的 JSON 有误：{m.group(0)[:120]}") from e
