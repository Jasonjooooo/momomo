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


def _ollama(c: dict, prompt: str, images: list[str] | None = None, timeout: int = 360) -> str:
    """先用 /api/chat（看图模型推荐的接口），不行再用 /api/generate；都不行把 Ollama 说的原因带出来"""
    base = str(c.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    msg = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    opts = {"temperature": 0.3 if images else 0.8, "num_ctx": 8192}
    tries = [("/api/chat", {"model": c.get("ollama_model"), "messages": [msg], "stream": False, "options": opts}),
             ("/api/generate", {"model": c.get("ollama_model"), "prompt": prompt, "stream": False, "options": opts,
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
    """本机看图模型：按时间顺序看 8 张图，说清楚发生了什么、看点在哪、画面上写了什么字、是什么情绪"""
    n = 8 if duration >= 6 else 5
    imgs = _jpeg_frames(path, duration, n=n, width=560)
    if not imgs:
        raise CaptionError("取不到画面")
    prompt = (
        f"下面是一条短视频从头到尾按时间顺序截的 {len(imgs)} 张图（第 1 张最早）。请用中文回答，每项一两句，只写看到的，不要编：\n"
        "1. 场景和人物：在哪、有谁（大概年龄、穿着、身份感）\n"
        "2. 经过：按时间顺序发生了什么，前后有什么变化\n"
        "3. 看点：整条视频最让人想看完的一刻是什么——反转、笑点、危险、离谱、厉害的地方，具体到动作\n"
        "4. 画面上的字：字幕、标题、牌子上的字，原样抄下来；没有就写「无」\n"
        "5. 情绪：人物的表情和整体氛围（好笑 / 尴尬 / 紧张 / 震惊 / 治愈……）\n"
        "不要提到「截图」「图片」「第几张」。"
    )
    try:
        text = _ollama(c, prompt, imgs)
    except CaptionError:
        imgs = _jpeg_frames(path, duration, n=3, width=448)      # 图少一点、小一点再试一次
        if not imgs:
            raise
        text = _ollama(c, prompt.replace(f"截的 {n} 张", f"截的 {len(imgs)} 张"), imgs)
    if len(text) < 6:
        raise CaptionError("本机模型没看出内容")
    return text[:900]


# ---------------------------------------------------------------- Gemini（可选，云端）
GEMINI = "https://generativelanguage.googleapis.com"
WATCH_PROMPT = (
    "这是一条短视频（画面和声音都在）。请用中文回答，每项一两句，只写看到和听到的，不要编：\n"
    "1. 场景和人物：在哪、有谁（大概年龄、穿着、身份感）\n"
    "2. 经过：按时间顺序发生了什么，前后有什么变化\n"
    "3. 看点：整条视频最让人想看完的一刻是什么——反转、笑点、危险、离谱、厉害的地方，具体到动作和第几秒\n"
    "4. 说的话：视频里人说了什么（原话，方言 / 外语翻成中文），背景音乐或音效有什么特点；没有就写「无」\n"
    "5. 画面上的字：字幕、标题、牌子上的字，原样抄下来；没有就写「无」\n"
    "6. 情绪：人物的表情和整体氛围（好笑 / 尴尬 / 紧张 / 震惊 / 治愈……）"
)


def gemini_ok(c: dict) -> tuple[bool, str]:
    key = str(c.get("gemini_key") or "").strip()
    if not key:
        return False, "没填 Gemini key"
    try:
        req = urllib.request.Request(f"{GEMINI}/v1beta/models?pageSize=1", headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=15, context=ssl_context()) as r:
            r.read()
        return True, ""
    except urllib.error.HTTPError as e:
        return False, "Gemini key 不对" if e.code in (400, 401, 403) else f"Gemini 返回错误 {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"连不上 Gemini（要开代理）：{e}"


def _g_req(url: str, key: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(url, data=data, headers={"x-goog-api-key": key, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def _gemini_upload(path: Path, key: str) -> dict:
    """视频超过 14MB：先传到 Gemini 的文件区（48 小时后它自己删），等它处理好"""
    import time

    size = path.stat().st_size
    with _g_req(f"{GEMINI}/upload/v1beta/files", key, json.dumps({"file": {"display_name": "clip"}}).encode(),
                {"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": "video/mp4",
                 "Content-Type": "application/json"}) as r:
        url = r.headers.get("X-Goog-Upload-URL")
    if not url:
        raise CaptionError("Gemini 没给上传地址")
    with _g_req(url, key, path.read_bytes(), {"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                                              "Content-Length": str(size)}, timeout=600) as r:
        f = json.loads(r.read())["file"]
    for _ in range(60):
        if f.get("state") == "ACTIVE":
            return f
        if f.get("state") == "FAILED":
            raise CaptionError("Gemini 处理视频失败")
        time.sleep(3)
        with _g_req(f"{GEMINI}/v1beta/{f['name']}", key) as r:
            f = json.loads(r.read())
    raise CaptionError("Gemini 处理视频太久")


def watch_gemini(path: Path, c: dict) -> str:
    """Gemini 看整条视频（画面 + 声音），按看点 / 说的话 / 字 / 情绪描述"""
    key = str(c.get("gemini_key") or "").strip()
    model = str(c.get("gemini_model") or "gemini-flash-latest").strip()
    try:
        if path.stat().st_size <= 14 * 1024 * 1024:
            media = {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(path.read_bytes()).decode()}}
        else:
            f = _gemini_upload(path, key)
            media = {"file_data": {"mime_type": f.get("mimeType") or "video/mp4", "file_uri": f["uri"]}}
        body = {"contents": [{"parts": [media, {"text": WATCH_PROMPT}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1500},
                "safetySettings": [{"category": k, "threshold": "BLOCK_ONLY_HIGH"} for k in
                                   ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                                    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]}
        with _g_req(f"{GEMINI}/v1beta/models/{model}:generateContent", key, json.dumps(body).encode(),
                    {"Content-Type": "application/json"}, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise CaptionError(f"Gemini 返回错误 {e.code}" + (f"：{msg[:150]}" if msg else "")) from e
    except CaptionError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CaptionError(f"连不上 Gemini（要开代理）：{e}") from e
    cands = data.get("candidates") or []
    text = "".join(p.get("text", "") for p in ((cands[0].get("content") or {}).get("parts") or [])) if cands else ""
    if len(text.strip()) < 6:
        why = (cands[0].get("finishReason") if cands else "") or (data.get("promptFeedback") or {}).get("blockReason") or ""
        raise CaptionError("Gemini 没看出内容" + (f"（{why}）" if why else ""))
    return text.strip()[:1500]


# ---------------------------------------------------------------- 写文案
def _prompt(desc: str, post_text: str, examples: list[str] | None = None) -> str:
    kinds = "\n".join(f"- {v}" for v in KINDS.values())
    ex = [e.strip() for e in (examples or []) if e and e.strip()][:20]
    return (
        "你是一个很会写推文的中文 X（推特）博主，在给一条短视频配一句发帖文案。\n\n"
        + (f"【视频内容】\n{desc}\n\n" if desc else "")
        + (f"【原帖文字】（可能是广告或乱码，只作参考）{_clean(post_text)[:300]}\n\n" if _clean(post_text) else "")
        + ("【这个账号以前的文案】（学它的口吻、用词、长短和节奏，不要照抄内容）\n" + "\n".join(f"- {e}" for e in ex) + "\n\n" if ex else "")
        + "【先判断类型】最适合下面哪一类：\n" + kinds + "\n\n"
        "【写 3 条，角度各不一样】比如：一条抓最大的看点设悬念，一条站在围观网友的角度说一句大实话，一条带点反差或调侃。\n"
        "好文案的样子：像真人刷到这条视频随口说的一句话；具体（说到视频里的人、动作、细节），不空泛；让人忍不住点开看完。\n"
        "不要写成这样：「太震撼了」「这也太离谱了吧」「看完沉默了」这类放到哪条视频都能用的空话；"
        "不要用「家人们」「绝绝子」「yyds」；不要以「视频」「这个视频」开头；不要把结局说破。\n"
        "硬性要求：每条 10 到 40 个字；不要话题标签（#）、@、链接；不要提来源、搬运、原作者；最多 1 个表情符号，也可以不用。\n\n"
        "最后从 3 条里挑你觉得最好的一条。\n"
        '只输出一行 JSON：{"kind": "六类之一", "captions": ["第一条", "第二条", "第三条"], "best": 最好那条的序号0到2}'
    )


def _tidy(cap: str) -> str:
    cap = re.sub(r"[#＃]\S+|@\w+|https?://\S+", "", str(cap or "")).strip().strip("「」\"“”")
    return re.sub(r"\s+", " ", cap)


def _parse(text: str) -> tuple[str, list[str], int]:
    """→ (类型, 3 条文案, 最好那条的序号)"""
    m = re.search(r"\{.*\}", text or "", re.S)
    kind, caps, best = "", [], 0
    if m:
        try:
            d = json.loads(m.group(0))
            kind = str(d.get("kind") or "")
            caps = d.get("captions") or ([d["caption"]] if d.get("caption") else [])
            best = int(d.get("best") or 0)
        except (ValueError, TypeError, KeyError):
            pass
    caps = [c for c in (_tidy(x) for x in caps if isinstance(x, str)) if len(c) >= 4]
    if not caps and (text or "").strip():
        caps = [c for c in (_tidy(x) for x in text.strip().splitlines()[:3]) if len(c) >= 4]
    if kind not in KINDS:
        kind = next((k for k in KINDS if k in (text or "")), "")
    best = best if 0 <= best < len(caps) else 0
    return kind, caps, best


def _deepseek(c: dict, prompt: str) -> str:
    key = str(c.get("api_key") or "").strip()
    base = str(c.get("api_base") or "https://api.deepseek.com").rstrip("/")
    try:
        data = _http(base + "/chat/completions", {"model": c.get("model") or "deepseek-chat", "temperature": 1.0, "max_tokens": 600,
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


def write(path: Path, duration: float, post_text: str, cfg: dict, examples: list[str] | None = None) -> dict:
    """返回 {caption, alts, kind, desc, how}：caption 是挑出来的那条，alts 是另外几条；how 说明是怎么写出来的"""
    c = settings(cfg)
    has_key = bool(str(c.get("api_key") or "").strip())
    ok, why = ollama_ok(c)
    desc, notes, seer = "", [], "本机看画面"
    if str(c.get("gemini_key") or "").strip():
        try:
            desc, seer = watch_gemini(path, c), "Gemini 看整条视频"
        except CaptionError as e:
            notes.append(str(e))
    if not desc and ok:
        try:
            desc = describe(path, duration, c)
        except CaptionError as e:
            notes.append(str(e))
    elif not desc:
        notes.append(why)
    if not desc and not _clean(post_text):
        raise CaptionError("；".join(notes) or "没有画面描述也没有原帖文字，写不出来")
    prompt = _prompt(desc, post_text, examples)
    if has_key:
        kind, caps, best = _parse(_deepseek(c, prompt))
        how = (f"{seer} + DeepSeek 写" + (f"（{notes[0]}，换成本机看）" if notes and desc else "")) if desc \
            else "DeepSeek 看原帖文字写（看画面没成功：" + "；".join(notes) + "）"
    elif ok:
        kind, caps, best = _parse(_ollama(c, prompt))
        how = "本机模型看画面并写（没填 DeepSeek key）"
    else:
        raise CaptionError("没填 DeepSeek key，本机模型也用不了：" + "；".join(notes))
    if not caps:
        raise CaptionError("AI 没写出有效的文案")
    if examples:
        how += f" · 参考了 {min(len(examples), 20)} 条范例"
    cap = caps[best][:120]
    return {"caption": cap, "alts": [x[:120] for k, x in enumerate(caps) if k != best], "kind": kind, "desc": desc, "how": how}
