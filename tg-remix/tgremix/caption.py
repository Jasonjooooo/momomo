"""AI 写发帖文案：本机的看图模型看画面 → DeepSeek 按 6 种口吻写一句中文文案

1. 本机 Ollama 里的看图模型（默认 qwen2.5vl:7b）看 4 帧画面，客观描述视频里发生了什么。画面不出你的电脑。
2. DeepSeek（和 AI 解说共用 key）拿到描述 + 原帖文字，判断是哪一类（吐槽 / 猎奇 / 搞笑 / 震撼人心 / 警示教育 / 工业工具），
   用对应的口吻写一句文案：口语、带悬念、不带话题标签、不标来源。发给 DeepSeek 的只有文字。
没有 DeepSeek key：由本机模型直接写；本机模型也没装：用 DeepSeek 只看原帖文字写；都没有：用原帖文字。
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .runtime import ffmpeg_bin, ssl_context

KINDS = {
    "吐槽": "吐槽：带点无语和调侃，像在跟朋友吐槽，比如「这操作我是真看不懂，有没有懂行的解释一下」",
    "猎奇": "猎奇：勾起好奇心，强调没见过、想不到，比如「活这么久第一次见这种东西，到底是干嘛用的」",
    "搞笑": "搞笑：轻松好笑，点出笑点但不剧透，比如「笑死，最后那一下谁顶得住」",
    "震撼人心": "震撼人心：情绪饱满、有感染力，比如「看完半天说不出话，这才是真正的勇气」",
    "警示教育": "警示教育：提醒风险，语气认真，比如「这种情况千万别学，一秒钟的事」",
    "工业工具": "工业工具：佩服工艺和效率，比如「原来工厂里是这么做的，效率太离谱了」",
}


class CaptionError(Exception):
    pass


def settings(cfg: dict) -> dict:
    """caption 设置；DeepSeek 的 key / 地址 / 模型没填就用 AI 解说里的"""
    from .config import DEFAULTS

    c = {**DEFAULTS["caption"], **(cfg.get("caption") or {})}
    vo = (cfg.get("process") or {}).get("voiceover") or {}
    for k in ("api_key", "api_base", "model"):
        if not str(c.get(k) or "").strip():
            c[k] = vo.get(k) or DEFAULTS["caption"].get(k) or ""
    return c


def _clean(text: str) -> str:
    t = re.sub(r"https?://\S+|t\.me/\S+|[#＃]\S+|@\w+", "", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _jpeg_frames(path: Path, duration: float, n: int = 4, width: int = 448) -> list[str]:
    out = []
    for i in range(n):
        t = duration * (0.1 + 0.8 * i / max(1, n - 1)) if duration else i
        try:
            r = subprocess.run([ffmpeg_bin("ffmpeg"), "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                                "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.stdout:
            out.append(base64.b64encode(r.stdout).decode())
    return out


def _http(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    ctx = ssl_context() if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- 本机 Ollama
def ollama_ok(c: dict) -> tuple[bool, str]:
    """Ollama 开着没有、模型下载了没有"""
    base = str(c.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False, "Ollama 没开（装好后打开 Ollama 这个软件）"
    want = str(c.get("ollama_model") or "")
    names = [m.get("name", "") for m in tags.get("models", [])]
    if not any(n == want or n == want + ":latest" for n in names):
        return False, f"Ollama 里还没有模型 {want}（在终端运行：ollama pull {want}）"
    return True, ""


def _ollama_err(e: Exception) -> str:
    """Ollama 出错时把它自己说的原因读出来（HTTP 400 之类光看状态码看不出问题）"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
            msg = (json.loads(body).get("error") if body.strip().startswith("{") else body) or ""
        except Exception:  # noqa: BLE001
            msg = ""
        return f"HTTP {e.code}" + (f"：{str(msg).strip()[:200]}" if msg else "")
    return str(e)


def _ollama(c: dict, prompt: str, images: list[str] | None = None, timeout: int = 240) -> str:
    """先用 /api/chat（看图模型推荐的接口），不行再用 /api/generate；都不行把 Ollama 说的原因带出来"""
    base = str(c.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    tries = [("/api/chat", {"model": c.get("ollama_model"), "messages": [msg], "stream": False, "options": {"temperature": 0.6}}),
             ("/api/generate", {"model": c.get("ollama_model"), "prompt": prompt, "stream": False, "options": {"temperature": 0.6},
                                **({"images": images} if images else {})})]
    errs = []
    for path, body in tries:
        try:
            data = _http(base + path, body, {}, timeout)
        except Exception as e:  # noqa: BLE001
            errs.append(_ollama_err(e))
            continue
        text = (data.get("message") or {}).get("content") if path == "/api/chat" else data.get("response")
        return str(text or "").strip()
    raise CaptionError("本机模型没回应：" + errs[-1])


def describe(path: Path, duration: float, c: dict) -> str:
    """本机看图模型：这条视频里发生了什么（2–3 句，客观）"""
    imgs = _jpeg_frames(path, duration)
    if not imgs:
        raise CaptionError("取不到画面")
    prompt = (f"这是一条短视频按时间顺序截的 {len(imgs)} 张图。请用 2 到 3 句中文，客观描述视频里发生了什么，"
              "重点写最有看点、最出乎意料的地方。只描述看到的内容，不要猜测，不要评价，不要提到「截图」「图片」。")
    try:
        text = _ollama(c, prompt, imgs)
    except CaptionError:
        imgs = _jpeg_frames(path, duration, n=2, width=336)      # 图少一点、小一点再试一次
        if not imgs:
            raise
        text = _ollama(c, prompt.replace("4 张", f"{len(imgs)} 张"), imgs)
    if len(text) < 6:
        raise CaptionError("本机模型没看出内容")
    return text[:400]


# ---------------------------------------------------------------- 写文案
def _prompt(desc: str, post_text: str) -> str:
    kinds = "\n".join(f"- {v}" for v in KINDS.values())
    return (
        "你在给一个中文 X（推特）账号写短视频的发帖文案。\n"
        + (f"视频内容：{desc}\n" if desc else "")
        + (f"原帖文字（可能是广告或乱码，只作参考）：{_clean(post_text)[:300]}\n" if _clean(post_text) else "")
        + "\n先判断这条视频最适合下面哪一类，再用这一类的口吻写一句文案：\n" + kinds + "\n\n"
        "要求：\n1. 一句话，15 到 40 个字，口语化，让人想点开看完\n2. 带一点悬念或情绪，不要把结局说破\n"
        "3. 不要话题标签（#），不要 @，不要链接，不要提来源、搬运、原作者\n4. 最多 1 个表情符号，也可以不用\n"
        "5. 不要出现「视频」「这个视频」开头\n"
        '只输出一行 JSON：{"kind": "六类之一", "caption": "文案"}'
    )


def _parse(text: str) -> tuple[str, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    kind, cap = "", ""
    if m:
        try:
            d = json.loads(m.group(0))
            kind, cap = str(d.get("kind") or ""), str(d.get("caption") or "")
        except ValueError:
            pass
    if not cap:
        cap = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    cap = re.sub(r"[#＃]\S+|@\w+|https?://\S+", "", cap).strip().strip("「」\"“”")
    if kind not in KINDS:
        kind = next((k for k in KINDS if k in (text or "")), "")
    return kind, cap


def _deepseek(c: dict, prompt: str) -> str:
    key = str(c.get("api_key") or "").strip()
    base = str(c.get("api_base") or "https://api.deepseek.com").rstrip("/")
    try:
        data = _http(base + "/chat/completions", {"model": c.get("model") or "deepseek-chat", "temperature": 1.0, "max_tokens": 200,
                                                  "messages": [{"role": "user", "content": prompt}]},
                     {"Authorization": f"Bearer {key}"}, 60)
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise CaptionError("DeepSeek 的 API Key 不对或已失效") from e
        if e.code == 402:
            raise CaptionError("DeepSeek 余额不足") from e
        raise CaptionError(f"DeepSeek 返回错误 {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise CaptionError(f"连不上 DeepSeek：{e}") from e


def write(path: Path, duration: float, post_text: str, cfg: dict) -> dict:
    """返回 {caption, kind, desc, how}；how 说明是怎么写出来的"""
    c = settings(cfg)
    has_key = bool(str(c.get("api_key") or "").strip())
    ok, why = ollama_ok(c)
    desc, notes = "", []
    if ok:
        try:
            desc = describe(path, duration, c)
        except CaptionError as e:
            notes.append(str(e))
    else:
        notes.append(why)
    if not desc and not _clean(post_text):
        raise CaptionError("；".join(notes) or "没有画面描述也没有原帖文字，写不出来")
    prompt = _prompt(desc, post_text)
    if has_key:
        kind, cap = _parse(_deepseek(c, prompt))
        how = "本机看画面 + DeepSeek 写" if desc else "DeepSeek 看原帖文字写（本机模型没用上：" + "；".join(notes) + "）"
    elif ok:
        kind, cap = _parse(_ollama(c, prompt))
        how = "本机模型看画面并写（没填 DeepSeek key）"
    else:
        raise CaptionError("没填 DeepSeek key，本机模型也用不了：" + "；".join(notes))
    if len(cap) < 4:
        raise CaptionError("AI 没写出有效的文案")
    return {"caption": cap[:120], "kind": kind, "desc": desc, "how": how}
