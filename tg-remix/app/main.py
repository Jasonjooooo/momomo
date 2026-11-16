"""TG 二创 · 图形界面入口

开发时运行：python app/main.py
打包后：双击 TGRemix.app
自检（打包流程里用）：TGRemix.app/Contents/MacOS/TGRemix --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(HERE.parent))  # 开发模式：能 import tgremix

BASE = Path(getattr(sys, "_MEIPASS", HERE))
DATA_ROOT = Path.home() / "TG二创"  # 配置、登录凭证、下载和成品都放在这里


def bundled_ffmpeg_dir() -> Path | None:
    """打包时 ffmpeg/ffprobe 放在 TGRemix.app/Contents/Resources/ffbin"""
    exe = Path(sys.executable).resolve()
    for d in (exe.parent.parent / "Resources" / "ffbin", BASE / "ffbin", HERE / "ffbin"):
        if (d / "ffmpeg").exists():
            return d
    return None


def check_https() -> bool:
    """真正用 HTTPS 连一次 Telegram 和 DeepSeek，确认证书验证没问题"""
    import asyncio
    import urllib.error
    import urllib.request

    import aiohttp

    from tgremix.runtime import ssl_context

    ok = True
    for url in ("https://api.telegram.org/", "https://api.deepseek.com/"):
        try:
            urllib.request.urlopen(url, timeout=20, context=ssl_context())
            print(f"HTTPS(urllib) {url}: ✅")
        except urllib.error.HTTPError as e:  # 能收到 HTTP 状态码说明证书验证通过
            print(f"HTTPS(urllib) {url}: ✅（HTTP {e.code}）")
        except Exception as e:  # noqa: BLE001
            print(f"HTTPS(urllib) {url}: ❌ {type(e).__name__}: {e}")
            ok = False

    async def via_aiohttp():
        conn = aiohttp.TCPConnector(ssl=ssl_context())
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.get("https://api.telegram.org/bot0:invalid/getMe") as r:
                return r.status

    try:
        print(f"HTTPS(aiohttp) api.telegram.org: ✅（HTTP {asyncio.run(via_aiohttp())}）")
    except Exception as e:  # noqa: BLE001
        print(f"HTTPS(aiohttp) api.telegram.org: ❌ {type(e).__name__}: {e}")
        ok = False
    return ok


def selftest() -> int:
    """不开窗口，检查打包结果：依赖能导入、ffmpeg 可用、能完整处理一条视频"""
    import subprocess
    import tempfile

    print("Python", sys.version.split()[0], "frozen =", getattr(sys, "frozen", False))
    import python_socks  # noqa: F401
    import telethon
    import webview  # noqa: F401
    import yaml  # noqa: F401

    print("telethon", telethon.__version__)
    from tgremix import runtime
    from tgremix.config import DEFAULTS, _merge
    from tgremix.processor import find_font, has_encoder, process_all, probe
    from tgremix.state import State

    ff = bundled_ffmpeg_dir()
    print("自带 ffmpeg 目录:", ff)
    if getattr(sys, "frozen", False) and not ff:
        print("❌ 应用里没有 ffmpeg")
        return 1
    runtime.set_ffmpeg_dir(ff)
    ffmpeg = runtime.ffmpeg_bin("ffmpeg")
    print("ffmpeg:", ffmpeg)
    print("videotoolbox 硬件编码:", has_encoder("h264_videotoolbox"), " libx264:", has_encoder("libx264"))
    font = find_font("")
    print("中文字体:", font)

    import edge_tts  # noqa: F401  （AI 配音依赖要打包进来）
    import aiohttp_socks  # noqa: F401  （机器人走代理要用）
    from tgremix.bot import FilterBot  # noqa: F401
    print("机器人组件: ✅")
    if not check_https():
        print("❌ HTTPS 证书验证失败")
        return 1

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dl = root / "downloads" / "selftest"
        dl.mkdir(parents=True)
        src = dl / "1.mp4"
        # 4 个镜头：测试图 3s / 彩条 3s / 0.3s 白闪 / 和第一个相同的测试图 2s
        subprocess.run([ffmpeg, "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=3",
                        "-f", "lavfi", "-i", "smptebars=s=1280x720:r=30:d=3",
                        "-f", "lavfi", "-i", "color=white:s=1280x720:r=30:d=0.3",
                        "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:d=8.3",
                        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]",
                        "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(src)],
                       check=True)
        sticker = root / "sticker.png"
        subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=120x120,format=rgba",
                        "-frames:v", "1", str(sticker)], check=True)
        (dl / "1.json").write_text(
            '{"channel":"selftest","id":1,"text":"自检标题 测试视频！#tag","link":"",'
            '"_commentary":"这是一段自检用的解说词，用来确认配音和字幕都正常。"}', encoding="utf-8")
        cfg = _merge(DEFAULTS, {"process": {
            "blur_boxes": [{"x": 0.73, "y": 0.02, "w": 0.25, "h": 0.1}], "zoom": 1.05,
            "filter": "cinematic", "vignette": True,
            "watermark": {"text": "@自检水印"},
            "edit": {"enabled": True, "transition": "fade", "transition_duration": 0.5},
            "stickers": [{"image": str(sticker), "position": "top_right", "width": 0.15, "animation": "float"}],
            "texts": [{"text": "关注我", "position": "bottom", "animation": "marquee"},
                      {"text": "逐字", "position": "center", "animation": "typewriter", "start": 1, "duration": 2}],
            "voiceover": {"enabled": True, "api_key": "unused"},
        }})
        cfg["_root"] = root
        ok = process_all(cfg, State(root / "state.json"))
        outs = list((root / "output").rglob("*.mp4"))
        if ok != 1 or not outs:
            print("❌ 处理失败")
            return 1
        info = probe(outs[0])
        print(f"成品: {info.width}x{info.height} {info.duration:.2f}s audio={info.has_audio}")
        # 3 + 3 + （去掉白闪和重复镜头）- 0.5 转场 = 5.5 秒，再除以默认变速 1.1
        expect = 5.5 / 1.1
        if (info.width, info.height) != (1080, 1920) or not info.has_audio or abs(info.duration - expect) > 0.3:
            print(f"❌ 成品规格不对（期望约 {expect:.2f} 秒）")
            return 1
        manifest = next((root / "output").rglob("清单.csv")).read_text(encoding="utf-8-sig")
        print("AI 配音:", "✅ 成功" if "自检用的解说词" in manifest else "⚠️ 没有生成（可能连不上微软语音服务）")
    print("✅ 自检通过")
    return 0


def main() -> None:
    from tgremix.runtime import ensure_ca_env

    ensure_ca_env()
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    import webview

    from api import Api  # noqa: E402  （app 目录在 sys.path 里）

    api = Api(DATA_ROOT, bundled_ffmpeg_dir())
    window = webview.create_window(
        "TG 二创",
        url=str(BASE / "ui" / "index.html"),
        js_api=api,
        width=1120,
        height=780,
        min_size=(920, 640),
    )
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()
