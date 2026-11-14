"""镜头剪辑：识别镜头切换 → 去掉太短 / 重复的镜头 → 按需限制总时长；并生成镜头之间的转场滤镜"""
from __future__ import annotations

import random
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .runtime import ffmpeg_bin, log

# 界面里的转场名称 → ffmpeg xfade 的转场
TRANSITIONS = {
    "fade": "fade",
    "dissolve": "dissolve",
    "slide": "slideleft",
    "slideup": "slideup",
    "wipe": "wipeleft",
    "circle": "circleopen",
    "zoom": "zoomin",
    "smooth": "smoothleft",
}


@dataclass
class Shot:
    start: float  # 相对于去掉片头后的时间（秒）
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def detect_cuts(src: Path, head: float, dur: float, sensitivity: float) -> list[float]:
    """返回镜头切换的时间点（相对于 head）"""
    threshold = min(0.9, max(0.05, 1.0 - float(sensitivity)))  # 灵敏度越高，阈值越低
    cmd = [ffmpeg_bin("ffmpeg"), "-hide_banner", "-ss", f"{head:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-an", "-vf", f"scale=320:-2,select='gt(scene,{threshold:.3f})',showinfo", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)})


def _dhash(src: Path, t: float) -> int | None:
    """某一帧的「差值哈希」，用来判断两个镜头画面是否基本相同"""
    cmd = [ffmpeg_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(src),
           "-frames:v", "1", "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True)
    px = r.stdout
    if len(px) < 72:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def plan_shots(src: Path, head: float, dur: float, ecfg: dict) -> list[Shot]:
    """决定保留哪些镜头；不需要剪辑时返回整段"""
    whole = [Shot(0.0, dur)]
    if not ecfg.get("enabled"):
        return whole
    cuts = [c for c in detect_cuts(src, head, dur, float(ecfg.get("sensitivity", 0.7))) if 0.2 < c < dur - 0.2]
    bounds = [0.0, *cuts, dur]
    shots = [Shot(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.05]
    total = len(shots)

    # 1) 去掉太短的镜头（闪一下的画面）
    min_len = float(ecfg.get("min_shot", 0.8))
    kept = [s for s in shots if s.length >= min_len] or [max(shots, key=lambda s: s.length)]
    short_removed = total - len(kept)

    # 2) 去掉重复镜头（例如回放、重复出现的画面）
    dup_removed = 0
    if ecfg.get("drop_duplicates", True) and len(kept) > 1:
        seen: list[int] = []
        uniq = []
        for s in kept:
            h = _dhash(src, head + s.start + s.length / 2)
            if h is not None and any(bin(h ^ o).count("1") <= 5 for o in seen):
                dup_removed += 1
                continue
            if h is not None:
                seen.append(h)
            uniq.append(s)
        kept = uniq or kept[:1]

    # 3) 限制总时长：按原顺序保留，超出的部分截掉
    max_total = float(ecfg.get("max_duration") or 0)
    if max_total > 0:
        out, acc = [], 0.0
        for s in kept:
            if acc >= max_total:
                break
            take = min(s.length, max_total - acc)
            if take >= min(min_len, 0.5):
                out.append(Shot(s.start, s.start + take))
                acc += take
        kept = out or kept[:1]

    log(f"    ✂️ 识别到 {total} 个镜头，去掉过短 {short_removed} 个、重复 {dup_removed} 个，保留 {len(kept)} 个")
    return kept


def transition_duration(ecfg: dict, shots: list[Shot]) -> float:
    """转场时长；镜头太短时自动缩短，保证每个镜头至少留一半画面"""
    if len(shots) < 2 or not ecfg.get("enabled") or ecfg.get("transition", "none") == "none":
        return 0.0
    d = float(ecfg.get("transition_duration", 0.5))
    return max(0.0, min(d, min(s.length for s in shots) / 2 - 0.05))


def build_segments(vin: str, ain: str | None, shots: list[Shot], ecfg: dict, fps: int) -> tuple[list[str], str, str | None, float]:
    """
    生成「切成镜头 → 转场拼接」的滤镜。
    返回：(滤镜列表, 视频输出标签, 音频输出标签, 新时长)
    只有一个镜头时直接返回输入。
    """
    if len(shots) == 1 and shots[0].start == 0:
        return [], vin, ain, shots[0].length
    n = len(shots)
    d = transition_duration(ecfg, shots)
    g: list[str] = []
    norm = f"fps={fps},settb=AVTB,format=yuv420p,setsar=1"
    if n == 1:
        s = shots[0]
        g.append(f"{vin}trim=start={s.start:.3f}:end={s.end:.3f},setpts=PTS-STARTPTS,{norm}[ev]")
        if ain:
            g.append(f"{ain}atrim=start={s.start:.3f}:end={s.end:.3f},asetpts=PTS-STARTPTS[ea]")
        return g, "[ev]", ("[ea]" if ain else None), s.length

    g.append(f"{vin}split={n}" + "".join(f"[sv{i}]" for i in range(n)))
    if ain:
        g.append(f"{ain}asplit={n}" + "".join(f"[sa{i}]" for i in range(n)))
    for i, s in enumerate(shots):
        g.append(f"[sv{i}]trim=start={s.start:.3f}:end={s.end:.3f},setpts=PTS-STARTPTS,{norm}[v{i}]")
        if ain:
            g.append(f"[sa{i}]atrim=start={s.start:.3f}:end={s.end:.3f},asetpts=PTS-STARTPTS,"
                     f"aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")

    names = list(TRANSITIONS.values())
    choice = ecfg.get("transition", "none")
    if d <= 0:
        g.append("".join(f"[v{i}]" + (f"[a{i}]" if ain else "") for i in range(n))
                 + f"concat=n={n}:v=1:a={1 if ain else 0}[ev]" + ("[ea]" if ain else ""))
        total = sum(s.length for s in shots)
    else:
        cur_v, cur_a = "[v0]", "[a0]"
        offset = 0.0
        for i in range(1, n):
            offset += shots[i - 1].length - d
            tr = random.choice(names) if choice == "random" else TRANSITIONS.get(choice, "fade")
            g.append(f"{cur_v}[v{i}]xfade=transition={tr}:duration={d:.3f}:offset={offset:.3f}[xv{i}]")
            cur_v = f"[xv{i}]"
            if ain:
                g.append(f"{cur_a}[a{i}]acrossfade=d={d:.3f}[xa{i}]")
                cur_a = f"[xa{i}]"
        g.append(f"{cur_v}null[ev]")
        if ain:
            g.append(f"{cur_a}anull[ea]")
        total = sum(s.length for s in shots) - d * (n - 1)
    return g, "[ev]", ("[ea]" if ain else None), total
