"""用 ffmpeg 批量处理视频：裁剪、变速、滤镜、画幅、标题、水印、片头片尾、背景音乐"""
from __future__ import annotations

import csv
import glob
import json
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import resolve
from .state import State

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}

FILTER_PRESETS = {
    "none": "",
    "bright": "eq=brightness=0.03:contrast=1.06:saturation=1.25",
    "cinematic": "eq=contrast=1.1:saturation=0.92,colorbalance=rs=-0.05:bs=0.07:rh=0.07:bh=-0.05",
    "vintage": "curves=preset=vintage,eq=saturation=0.85",
    "cool": "colorbalance=rs=-0.04:bs=0.08:bm=0.05,eq=saturation=1.05",
    "warm": "colorbalance=rs=0.07:rm=0.05:bs=-0.05:bm=-0.03,eq=saturation=1.08",
    "bw": "hue=s=0,eq=contrast=1.1",
}

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


class ProcessError(Exception):
    pass


# ---------------------------------------------------------------- 基础工具
def check_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("找不到 ffmpeg，请先安装：brew install ffmpeg")


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: Path) -> MediaInfo:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise ProcessError(f"无法读取视频：{r.stderr.strip()[-300:]}")
    data = json.loads(r.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ProcessError("文件里没有视频画面")
    w, h = int(v["width"]), int(v["height"])
    # 手机竖拍的视频常带旋转信息，ffmpeg 解码时会自动转正，这里同步宽高
    rot = int(v.get("tags", {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return MediaInfo(dur, w, h, has_audio)


def find_font(cfg_font: str) -> str | None:
    if cfg_font:
        if Path(cfg_font).expanduser().exists():
            return str(Path(cfg_font).expanduser())
        print(f"⚠️ 配置的字体不存在：{cfg_font}，改为自动查找")
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    # 新版 macOS 的苹方在系统资源目录里
    hits = glob.glob("/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc")
    return hits[0] if hits else None


_encoder_cache: dict[str, bool] = {}


def has_encoder(name: str) -> bool:
    if name not in _encoder_cache:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        _encoder_cache[name] = name in r.stdout
    return _encoder_cache[name]


def video_codec_args(p: dict, software: bool = False) -> list[str]:
    enc = p.get("encoder", "auto")
    if not software and enc in ("auto", "videotoolbox") and has_encoder("h264_videotoolbox"):
        return ["-c:v", "h264_videotoolbox", "-b:v", str(p["video_bitrate"]), "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]


def run_ffmpeg(args: list[str], p: dict) -> None:
    """先用硬件编码，失败自动改用软件编码"""
    for software in (False, True):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args[:-1],
               *video_codec_args(p, software), *AUDIO_ARGS,
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", args[-1]]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
        uses_hw = "h264_videotoolbox" in cmd
        if not uses_hw:
            raise ProcessError(r.stderr.strip()[-800:] or "ffmpeg 处理失败")
        print("    ⚠️ 硬件编码失败，改用软件编码重试")


def even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def q(path: str) -> str:
    """滤镜参数里的文件路径：用单引号包起来，转义特殊字符"""
    return "'" + path.replace("\\", "/").replace("'", r"'\''") + "'"


# ---------------------------------------------------------------- 标题文字
_EMOJI = re.compile(
    "[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3\U000E0000-\U000E007F]"
)


def clean_title(text: str) -> str:
    """从 TG 帖子文字里取第一句当标题：去掉链接、话题标签、@、表情"""
    t = re.sub(r"https?://\S+|t\.me/\S+", "", text or "")
    t = re.sub(r"[#＃]\S+", "", t)
    t = re.sub(r"@\w+", "", t)
    t = _EMOJI.sub("", t)
    for line in t.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" -—_|·•:：,，")
        if len(line) >= 2:
            first = re.split(r"(?<=[。！？!?])", line)[0].strip()
            return first or line
    return ""


def _char_w(c: str) -> float:
    return 0.55 if ord(c) < 0x2E80 else 1.0


def wrap_title(text: str, per_line: int, max_lines: int) -> list[str]:
    lines, cur, width = [], "", 0.0
    for c in text:
        cw = _char_w(c)
        if width + cw > per_line and cur:
            lines.append(cur.strip())
            cur, width = "", 0.0
        cur += c
        width += cw
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines


# ---------------------------------------------------------------- 主处理
@dataclass
class Job:
    src: Path
    meta: dict
    key: str


def _pick_bgm(cfg: dict) -> Path | None:
    p = cfg["process"].get("bgm")
    if not p:
        return None
    path = resolve(cfg, p)
    if path.is_dir():
        files = [f for f in path.iterdir() if f.suffix.lower() in AUDIO_EXTS]
        return random.choice(files) if files else None
    return path if path.exists() else None


def _canvas_size(layout: str, cw: float, ch: float) -> tuple[int, int]:
    if layout == "vertical":
        return 1080, 1920
    if layout == "horizontal":
        return 1920, 1080
    k = min(1.0, 1920 / max(cw, ch))  # keep：长边最多 1920
    return even(cw * k), even(ch * k)


def render_main(job: Job, out: Path, cfg: dict, font: str | None, tmpdir: Path, seconds: float | None) -> float:
    """处理一条视频（不含片头片尾），返回成品时长"""
    p = cfg["process"]
    info = probe(job.src)
    head, tail = float(p["trim_head"] or 0), float(p["trim_tail"] or 0)
    dur = info.duration - head - tail
    if seconds:
        dur = min(dur, seconds)
    if dur < 1:
        raise ProcessError(f"去掉片头片尾后不足 1 秒（原视频 {info.duration:.1f} 秒）")
    speed = min(2.0, max(0.5, float(p["speed"] or 1)))
    out_dur = dur / speed

    inputs: list[str] = ["-ss", f"{head:.3f}", "-t", f"{dur:.3f}", "-i", str(job.src)]
    n_in = 1
    graph: list[str] = []
    cur = "[0:v]"

    # 1) 模糊指定区域（坐标相对原画面）
    for i, b in enumerate(p.get("blur_boxes") or []):
        bx, by = even(float(b["x"]) * info.width), even(float(b["y"]) * info.height)
        bw = min(even(float(b["w"]) * info.width), info.width - bx)
        bh = min(even(float(b["h"]) * info.height), info.height - by)
        if bw < 4 or bh < 4:
            continue
        r = max(1, min(20, min(bw, bh) // 4 - 1))
        graph.append(f"{cur}split[b{i}a][b{i}b];[b{i}b]crop={bw}:{bh}:{bx}:{by},boxblur={r}:2[b{i}c];"
                     f"[b{i}a][b{i}c]overlay={bx}:{by}[b{i}o]")
        cur = f"[b{i}o]"

    # 2) 裁边、放大、变速、调色、锐化、暗角
    chain: list[str] = []
    c = p["crop"]
    t, btm, l, r_ = (float(c.get(k, 0) or 0) for k in ("top", "bottom", "left", "right"))
    if any((t, btm, l, r_)):
        chain.append(f"crop=trunc(iw*{1 - l - r_:.4f}/2)*2:trunc(ih*{1 - t - btm:.4f}/2)*2:trunc(iw*{l:.4f}):trunc(ih*{t:.4f})")
    cw, ch = info.width * (1 - l - r_), info.height * (1 - t - btm)
    zoom = float(p.get("zoom") or 1)
    if zoom > 1.001:
        chain.append(f"scale=trunc(iw*{zoom:.4f}/2)*2:trunc(ih*{zoom:.4f}/2)*2,crop=trunc(iw/{zoom:.4f}/2)*2:trunc(ih/{zoom:.4f}/2)*2")
    if abs(speed - 1) > 0.001:
        chain.append(f"setpts=PTS/{speed:.4f}")
    preset = FILTER_PRESETS.get(str(p.get("filter") or "none"))
    if preset is None:
        raise ProcessError(f"未知的滤镜预设：{p.get('filter')}（可选：{'、'.join(FILTER_PRESETS)}）")
    if preset:
        chain.append(preset)
    if p.get("sharpen"):
        chain.append("unsharp=5:5:0.6:5:5:0")
    if p.get("vignette"):
        chain.append("vignette=PI/5")
    chain.append(f"fps={int(p['fps'])},setsar=1")
    graph.append(f"{cur}{','.join(chain)}[base]")

    # 3) 画幅：原画面居中，空白处用模糊放大的画面填充
    W, H = _canvas_size(p["layout"], cw, ch)
    fit = min(W / cw, H / ch)
    fg_w, fg_h = even(cw * fit), even(ch * fit)
    if p["layout"] in ("vertical", "horizontal") and (fg_w < W - 4 or fg_h < H - 4):
        sw, sh = even(W / 4), even(H / 4)
        graph.append(
            f"[base]split[bgs][fgs];"
            f"[bgs]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},gblur=sigma=10,"
            f"scale={W}:{H},eq=brightness=-0.08[bg];"
            f"[fgs]scale={fg_w}:{fg_h}[fg];"
            f"[bg][fg]overlay=({W}-w)/2:({H}-h)/2[v0]"
        )
    else:
        fg_w, fg_h = W, H
        graph.append(f"[base]scale={W}:{H}[v0]")
    cur = "[v0]"
    k = min(W, H) / 1080  # 字号按 1080 宽设计，画面更小时等比缩小

    # 4) 标题
    tcfg = p["title"]
    title = ""
    if tcfg.get("enabled"):
        title = clean_title(job.meta.get("text", "")) if tcfg.get("source") == "post" else str(tcfg.get("text") or "")
        if tcfg.get("source") == "post" and not title and tcfg.get("text"):
            title = str(tcfg["text"])  # 帖子没有文字时用固定标题兜底
    if title:
        if not font:
            raise ProcessError("找不到中文字体，无法添加标题；请在 config.yaml 的 process.font 填写字体文件路径")
        fs = max(16, int(int(tcfg["font_size"]) * k))
        lines = wrap_title(title, int(tcfg["max_chars_per_line"]), int(tcfg["max_lines"]))
        line_h = int(fs * 1.35)
        block_h = line_h * len(lines)
        top_space = (H - fg_h) / 2
        y0 = int((top_space - block_h) / 2) if top_space >= block_h + 40 else int(H * 0.06)
        parts = []
        for i, line in enumerate(lines):
            tf = tmpdir / f"title{i}.txt"
            tf.write_text(line, encoding="utf-8")
            box = f":box=1:boxcolor=black@0.45:boxborderw={max(6, int(14 * k))}" if tcfg.get("box") else ":borderw=3:bordercolor=black@0.6"
            parts.append(
                f"drawtext=fontfile={q(font)}:textfile={q(str(tf))}:fontsize={fs}:fontcolor={tcfg['color']}"
                f":x=(w-text_w)/2:y={y0 + i * line_h}{box}"
            )
        graph.append(f"{cur}{','.join(parts)}[v1]")
        cur = "[v1]"

    # 5) 水印
    wm = p["watermark"]
    pos = wm.get("position", "bottom_right")
    margin = int(W * 0.04)
    xr = "left" in pos
    yt = "top" in pos
    if wm.get("text"):
        if not font:
            raise ProcessError("找不到中文字体，无法添加文字水印")
        tf = tmpdir / "wm.txt"
        tf.write_text(str(wm["text"]), encoding="utf-8")
        x = f"{margin}" if xr else f"w-text_w-{margin}"
        y = f"{margin}" if yt else f"h-text_h-{margin}"
        graph.append(
            f"{cur}drawtext=fontfile={q(font)}:textfile={q(str(tf))}:fontsize={max(12, int(int(wm['font_size']) * k))}"
            f":fontcolor=white@{float(wm['opacity']):.2f}:borderw=2:bordercolor=black@{float(wm['opacity']) * 0.6:.2f}"
            f":x={x}:y={y}[v2]"
        )
        cur = "[v2]"
    if wm.get("image"):
        img = resolve(cfg, wm["image"])
        if not img.exists():
            raise ProcessError(f"水印图片不存在：{img}")
        inputs += ["-i", str(img)]
        iw = even(W * float(wm["image_width"]))
        x = f"{margin}" if xr else f"W-w-{margin}"
        y = f"{margin}" if yt else f"H-h-{margin}"
        if wm.get("text") and not yt:
            y = f"H-h-{margin + int(int(wm['font_size']) * k * 1.6)}"  # 文字水印在下面，图片往上挪
        graph.append(f"[{n_in}:v]scale={iw}:-1,format=rgba,colorchannelmixer=aa={float(wm['opacity']):.2f}[wmi];"
                     f"{cur}[wmi]overlay={x}:{y}[v3]")
        n_in += 1
        cur = "[v3]"
    graph.append(f"{cur}format=yuv420p[vout]")

    # 6) 声音：原声变速 + 背景音乐
    ov = float(p.get("original_volume", 1))
    bgm = _pick_bgm(cfg)
    if info.has_audio:
        atempo = f"atempo={speed:.4f}," if abs(speed - 1) > 0.001 else ""
        graph.append(f"[0:a]{atempo}volume={ov:.2f},aresample=44100[a0]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{out_dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        graph.append(f"[{n_in}:a]anull[a0]")
        n_in += 1
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]
        graph.append(f"[{n_in}:a]volume={float(p['bgm_volume']):.2f},aresample=44100[abg];"
                     f"[a0][abg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        n_in += 1
    else:
        graph.append("[a0]anull[aout]")

    args = [*inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
            "-t", f"{out_dur:.3f}", "-r", str(int(p["fps"])), str(out)]
    run_ffmpeg(args, p)
    job.meta["_title"] = title
    job.meta["_bgm"] = bgm.name if bgm else ""
    return out_dur


def add_intro_outro(main: Path, out: Path, cfg: dict) -> None:
    p = cfg["process"]
    parts = []
    for k in ("intro", "outro"):
        if p.get(k):
            path = resolve(cfg, p[k])
            if not path.exists():
                raise ProcessError(f"{'片头' if k == 'intro' else '片尾'}文件不存在：{path}")
            parts.append((k, path))
    if not parts:
        shutil.move(str(main), str(out))
        return
    mi = probe(main)
    W, H, fps = mi.width, mi.height, int(p["fps"])
    files = ([pp for k, pp in parts if k == "intro"] + [main] + [pp for k, pp in parts if k == "outro"])
    inputs, graph, labels = [], [], []
    for i, f in enumerate(files):
        info = probe(f)
        inputs += ["-i", str(f)]
        graph.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                     f"setsar=1,fps={fps},format=yuv420p[v{i}]")
        if info.has_audio:
            graph.append(f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            graph.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={info.duration:.3f}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    graph.append(f"{''.join(labels)}concat=n={len(files)}:v=1:a=1[vout][aout]")
    run_ffmpeg([*inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", str(out)], p)


# ---------------------------------------------------------------- 批量
def collect_jobs(cfg: dict) -> list[Job]:
    """downloads 目录里所有视频；有同名 .json 的是从 TG 下载的，没有的当作本地视频"""
    root = resolve(cfg, cfg["paths"]["downloads"])
    jobs = []
    if not root.exists():
        return jobs
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in VIDEO_EXTS or ".part" in f.name:
            continue
        meta_file = f.with_suffix(".json")
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        rel = f.relative_to(root)
        meta.setdefault("channel", rel.parts[0] if len(rel.parts) > 1 else "本地")
        meta.setdefault("id", f.stem)
        meta.setdefault("text", "")
        meta.setdefault("link", "")
        jobs.append(Job(f, meta, str(rel.with_suffix(""))))
    return jobs


def _append_manifest(path: Path, row: dict) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def process_one(job: Job, cfg: dict, font: str | None, out_dir: Path, seconds: float | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", f"{job.meta['channel']}_{job.meta['id']}")
    out = out_dir / f"{name}.mp4"
    with tempfile.TemporaryDirectory(prefix="tgremix_") as td:
        tmpdir = Path(td)
        main = tmpdir / "main.mp4"
        render_main(job, main, cfg, font, tmpdir, seconds)
        add_intro_outro(main, out, cfg)
    return out


def process_all(cfg: dict, state: State, redo: bool = False, limit: int | None = None,
                only: Path | None = None, seconds: float | None = None) -> int:
    check_ffmpeg()
    font = find_font(cfg["process"].get("font", ""))
    if not font:
        print("⚠️ 没找到中文字体，标题和文字水印会失败；可在 config.yaml 的 process.font 指定字体文件")

    if only:
        if not only.exists():
            raise SystemExit(f"文件不存在：{only}")
        meta_file = only.with_suffix(".json")
        meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        meta.setdefault("channel", "本地")
        meta.setdefault("id", only.stem)
        meta.setdefault("text", "")
        jobs = [Job(only, meta, f"file:{only}")]
        redo = True
    else:
        jobs = [j for j in collect_jobs(cfg) if redo or not state.is_processed(j.key)]
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        print("没有需要处理的新视频")
        return 0

    out_dir = resolve(cfg, cfg["paths"]["output"]) / datetime.now().strftime("%Y-%m-%d")
    ok = 0
    print(f"🎬 开始处理 {len(jobs)} 个视频 → {out_dir}")
    for i, job in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {job.key}")
        started = datetime.now()
        try:
            out = process_one(job, cfg, font, out_dir, seconds)
        except ProcessError as e:
            print(f"    ❌ 失败：{e}")
            continue
        secs = (datetime.now() - started).total_seconds()
        print(f"    ✅ {out.name}（用时 {secs:.0f} 秒）")
        ok += 1
        if not only:
            state.mark_processed(job.key)
            state.save()
        _append_manifest(out_dir / "清单.csv", {
            "处理时间": started.strftime("%Y-%m-%d %H:%M"),
            "频道": job.meta.get("channel_title") or job.meta.get("channel", ""),
            "原链接": job.meta.get("link", ""),
            "原文": (job.meta.get("text") or "").replace("\n", " ")[:300],
            "标题": job.meta.get("_title", ""),
            "背景音乐": job.meta.get("_bgm", ""),
            "成品": out.name,
        })
    print(f"\n✅ 处理完成：成功 {ok} 个，失败 {len(jobs) - ok} 个")
    return ok
