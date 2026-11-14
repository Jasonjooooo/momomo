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

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dl = root / "downloads" / "selftest"
        dl.mkdir(parents=True)
        src = dl / "1.mp4"
        subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-c:a", "aac",
                        str(src)], check=True)
        (dl / "1.json").write_text('{"channel":"selftest","id":1,"text":"自检标题 测试视频！#tag","link":""}',
                                   encoding="utf-8")
        cfg = _merge(DEFAULTS, {"process": {
            "trim_head": 1, "blur_boxes": [{"x": 0.73, "y": 0.02, "w": 0.25, "h": 0.1}], "zoom": 1.05,
            "filter": "cinematic", "vignette": True,
            "watermark": {"text": "@自检水印"},
        }})
        cfg["_root"] = root
        ok = process_all(cfg, State(root / "state.json"))
        outs = list((root / "output").rglob("*.mp4"))
        if ok != 1 or not outs:
            print("❌ 处理失败")
            return 1
        info = probe(outs[0])
        print(f"成品: {info.width}x{info.height} {info.duration:.2f}s audio={info.has_audio}")
        if (info.width, info.height) != (1080, 1920) or not info.has_audio:
            print("❌ 成品规格不对")
            return 1
    print("✅ 自检通过")
    return 0


def main() -> None:
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
