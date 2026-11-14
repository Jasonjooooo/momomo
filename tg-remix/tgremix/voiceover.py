"""AI 解说配音：大模型写解说词 → 微软免费语音（edge-tts）朗读 → 返回音频和字幕时间轴"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

STYLES = {
    "humor": "幽默风趣，带点调侃",
    "sharp": "犀利点评，观点鲜明",
    "warm": "温暖走心，有共鸣",
    "neutral": "客观简洁，像新闻播报",
    "story": "讲故事的口吻，有悬念",
}

VOICES = {
    "zh-CN-YunxiNeural": "云希（男，年轻活泼）",
    "zh-CN-YunjianNeural": "云健（男，激情解说）",
    "zh-CN-YunyangNeural": "云扬（男，新闻播报）",
    "zh-CN-XiaoxiaoNeural": "晓晓（女，温柔）",
    "zh-CN-XiaoyiNeural": "晓伊（女，活泼）",
    "zh-CN-liaoning-XiaobeiNeural": "晓北（女，东北话）",
    "zh-TW-HsiaoChenNeural": "曉臻（女，台湾腔）",
}


class VoiceoverError(Exception):
    pass


def _clean(text: str) -> str:
    t = re.sub(r"https?://\S+|t\.me/\S+|[#＃]\S+|@\w+", "", text or "")
    return re.sub(r"\s+", " ", t).strip()


def write_commentary(post_text: str, seconds: float, vcfg: dict) -> str:
    """调用 OpenAI 兼容接口（默认 DeepSeek）写一段解说词"""
    key = str(vcfg.get("api_key") or "").strip()
    if not key:
        raise VoiceoverError("没有填写 AI 接口的 API Key")
    base = str(vcfg.get("api_base") or "https://api.deepseek.com").rstrip("/")
    url = base + "/chat/completions"
    max_chars = max(12, int(seconds * 4.0))
    style = STYLES.get(vcfg.get("style", "humor"), vcfg.get("style") or "")
    extra = str(vcfg.get("extra") or "").strip()
    prompt = (
        f"下面是一条短视频配的原始文字说明：\n「{_clean(post_text)[:600]}」\n\n"
        f"请为这条视频写一段中文口播解说词，视频约 {seconds:.0f} 秒。要求：\n"
        f"1. 风格：{style}\n"
        f"2. 不超过 {max_chars} 个字，口语化，适合朗读\n"
        f"3. 加入你自己的观点或感受，不要只复述原文\n"
        f"4. 不要表情符号、话题标签、引号、括号，不要以“这个视频”开头\n"
        + (f"5. 额外要求：{extra}\n" if extra else "")
        + "只输出解说词本身。"
    )
    body = json.dumps({
        "model": vcfg.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": 400,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:200]
        if e.code == 401:
            raise VoiceoverError("API Key 不对或已失效") from e
        if e.code == 402:
            raise VoiceoverError("AI 接口余额不足，请充值") from e
        raise VoiceoverError(f"AI 接口返回错误 {e.code}：{detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VoiceoverError(f"连不上 AI 接口（{base}）：{e}") from e
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise VoiceoverError(f"AI 接口返回格式不对：{str(data)[:200]}") from e
    text = re.sub(r"[\"“”「」『』（）()#*]", "", text).strip()
    if len(text) > max_chars * 1.3:
        cut = text[: int(max_chars * 1.2)]
        m = re.search(r".*[。！？!?]", cut)
        text = m.group(0) if m else cut
    if len(text) < 4:
        raise VoiceoverError("AI 没有写出有效的解说词")
    return text


async def _tts(text: str, voice: str, rate: str, out: Path) -> list[tuple[float, float, str]]:
    import edge_tts

    cues = []
    com = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    with open(out, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                start = chunk["offset"] / 1e7
                cues.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
    return cues


def synthesize(text: str, vcfg: dict, out: Path) -> list[tuple[float, float, str]]:
    """朗读解说词，保存成 mp3；返回每句话的 (开始, 结束, 文字)"""
    voice = vcfg.get("voice") or "zh-CN-YunxiNeural"
    pct = int(round((float(vcfg.get("rate", 1.1)) - 1) * 100))
    rate = f"{pct:+d}%"
    try:
        cues = asyncio.run(_tts(text, voice, rate, out))
    except Exception as e:  # noqa: BLE001
        raise VoiceoverError(f"AI 配音失败（需要能访问微软语音服务）：{e}") from e
    if not out.exists() or out.stat().st_size < 1000:
        raise VoiceoverError("AI 配音没有生成声音")
    if not cues:
        cues = [(0.0, 0.0, text)]
    return cues


def split_cue_text(text: str, per_line: int = 16) -> list[str]:
    """一句话太长时，按标点或字数拆成几段字幕"""
    parts = [p for p in re.split(r"(?<=[，,、；;。！？!?])", text) if p.strip()]
    out = []
    for p in parts:
        p = p.strip()
        while len(p) > per_line:
            out.append(p[:per_line])
            p = p[per_line:]
        if p:
            out.append(p)
    return out or [text]
