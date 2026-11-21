"""裸露检查：在本机用开源的 NudeNet 模型（MIT 协议，320n.onnx，约 12MB）看视频的几帧画面

标准（你定的）：擦边可以隐晦，但不能有裸露 ——
  胸、臀、下体、肛门任何一个「露出来」的 → 不要；穿着泳装 / 紧身衣这类「遮着的」→ 可以过。
画面只在你电脑上判断，不传到任何地方。
预处理和 NudeNet 自带的一样（补成正方形 → 缩到 320×320 → RGB）：ffmpeg 取帧、补成正方形，
缩小用 numpy 照 OpenCV 的 INTER_LINEAR 算法来做（结果和 NudeNet 原版几乎一样），不用装 OpenCV。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .runtime import ffmpeg_bin

LABELS = [
    "FEMALE_GENITALIA_COVERED", "FACE_FEMALE", "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "MALE_BREAST_EXPOSED", "ANUS_EXPOSED", "FEET_EXPOSED", "BELLY_COVERED", "FEET_COVERED", "ARMPITS_COVERED",
    "ARMPITS_EXPOSED", "FACE_MALE", "BELLY_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_COVERED", "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
]
# 算「裸露」的：露出来的胸、臀、下体、肛门（男性上身、肚子、腋下、脚不算）
BLOCK = {"FEMALE_BREAST_EXPOSED": "露胸", "BUTTOCKS_EXPOSED": "露臀", "FEMALE_GENITALIA_EXPOSED": "露下体",
         "MALE_GENITALIA_EXPOSED": "露下体", "ANUS_EXPOSED": "露下体"}
THRESHOLD = 0.45      # 模型把握度 ≥ 45% 才算（NudeNet 自己报框用 0.2，这里更严一点，少误判）
SIZE = 320

_session = None
_err = ""


def model_path() -> Path | None:
    """模型放在哪：打包后在 TGRemix.app/Contents/Resources/models；开发时在 tg-remix/models"""
    cands = [os.environ.get("TGREMIX_NSFW_MODEL", "")]
    exe = Path(sys.executable).resolve()
    cands += [exe.parent.parent / "Resources" / "models" / "320n.onnx",
              Path(getattr(sys, "_MEIPASS", "/nonexistent")) / "models" / "320n.onnx",
              Path(__file__).resolve().parent.parent / "models" / "320n.onnx"]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def available() -> tuple[bool, str]:
    """能不能用：要有 onnxruntime 和模型文件（1.10 以后的完整安装包里都有）"""
    global _session, _err
    if _session is not None:
        return True, ""
    if _err:
        return False, _err
    try:
        import numpy  # noqa: F401
        import onnxruntime as ort
    except Exception:  # noqa: BLE001
        _err = "这个版本的安装包里没有裸露检查组件，要下载新版完整安装包"
        return False, _err
    mp = model_path()
    if not mp:
        _err = "找不到裸露检查模型（320n.onnx），要下载新版完整安装包"
        return False, _err
    try:
        so = ort.SessionOptions()
        so.log_severity_level = 3
        _session = ort.InferenceSession(str(mp), so, providers=["CPUExecutionProvider"])
    except Exception as e:  # noqa: BLE001
        _err = f"裸露检查模型加载失败：{e}"
        return False, _err
    return True, ""


def _resize_linear(img, size: int):
    """和 OpenCV 的 cv2.resize(INTER_LINEAR) 一样的缩放（不抗锯齿：每个点只看最近的 2×2 个像素）"""
    import numpy as np

    h, w = img.shape[:2]

    def axis(n_in):
        x = (np.arange(size) + 0.5) * (n_in / size) - 0.5
        x0 = np.floor(x).astype(int)
        f = (x - x0).astype(np.float32)
        x0c, x1c = np.clip(x0, 0, n_in - 1), np.clip(x0 + 1, 0, n_in - 1)
        f[x < 0] = 0
        return x0c, x1c, f

    y0, y1, fy = axis(h)
    x0, x1, fx = axis(w)
    a = img.astype(np.float32)
    top = a[y0][:, x0] * (1 - fx)[None, :, None] + a[y0][:, x1] * fx[None, :, None]
    bot = a[y1][:, x0] * (1 - fx)[None, :, None] + a[y1][:, x1] * fx[None, :, None]
    out = top * (1 - fy)[:, None, None] + bot * fy[:, None, None]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _square_rgb(path: Path, t: float | None):
    """取一帧，补成正方形（右边 / 下边补黑），返回 numpy 数组（RGB）"""
    import numpy as np

    pre = ["-ss", f"{t:.2f}"] if t is not None else []
    r = subprocess.run([ffmpeg_bin("ffmpeg"), "-v", "error", *pre, "-i", str(path), "-frames:v", "1",
                        "-vf", "pad='max(iw,ih)':'max(iw,ih)':0:0:black,format=rgb24", "-f", "image2pipe", "-vcodec", "ppm", "-"],
                       capture_output=True, timeout=60)
    data = r.stdout
    if not data.startswith(b"P6"):
        return None
    parts = data.split(maxsplit=4)                       # P6 宽 高 255 像素
    w, h = int(parts[1]), int(parts[2])
    px = parts[4]
    if len(px) < w * h * 3:
        return None
    return np.frombuffer(px[: w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def _frames(path: Path, duration: float, n: int) -> list[tuple[float, bytes]]:
    """均匀取 n 帧（避开开头结尾 5%），每帧补成正方形再缩到 320×320，返回 RGB 原始数据"""
    out = []
    for i in range(n):
        t = duration * (0.05 + 0.9 * (i + 0.5) / n) if duration else 0.5 * i
        try:
            img = _square_rgb(path, t)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if img is not None:
            out.append((t, _resize_linear(img, SIZE).tobytes()))
    return out


def scores(rgb: bytes) -> dict[str, float]:
    """一帧画面里每一类的最高把握度"""
    import numpy as np

    img = np.frombuffer(rgb, dtype=np.uint8).reshape(SIZE, SIZE, 3).astype(np.float32) / 255.0
    # NudeNet 原版的预处理实际喂给模型的是 BGR 顺序（它先 RGBA2BGR 又 swapRB，红蓝换了两次又换回来），照它的来，判断结果才和原版一样
    blob = np.ascontiguousarray(img[:, :, ::-1].transpose(2, 0, 1))[None]
    inp = _session.get_inputs()[0].name
    out = _session.run(None, {inp: blob})[0]           # (1, 4 + 18, N)
    cls = np.squeeze(out).T[:, 4:]                      # (N, 18)
    best = cls.max(axis=0)
    return {LABELS[i]: float(best[i]) for i in range(len(LABELS))}


def check(path: Path, duration: float, n: int | None = None) -> dict:
    """返回 {ok: 检查了没有, nude: 有没有裸露, hits: [(哪一帧, 什么, 把握度)], frames: 看了几帧, error}"""
    ok, err = available()
    if not ok:
        return {"ok": False, "nude": False, "hits": [], "frames": 0, "error": err}
    n = n or (8 if duration <= 60 else 12)
    hits = []
    frames = _frames(path, duration, n)
    for t, rgb in frames:
        sc = scores(rgb)
        for k, name in BLOCK.items():
            if sc.get(k, 0) >= THRESHOLD:
                hits.append((round(t, 1), name, round(sc[k], 2)))
    return {"ok": bool(frames), "nude": bool(hits), "hits": hits, "frames": len(frames),
            "error": "" if frames else "取不到画面"}
