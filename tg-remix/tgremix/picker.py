"""选片：给素材打「推荐分」，只要合适的视频

- TG 频道 / 群组的视频：下载前就打分（比这个群平时火几倍、涨得快不快、转发、几个群都在发、时长、清晰度、广告词），
  只下载分最高的几条；不合格的不下载、不处理
- 收件箱：智Tool 等插件下到「下载/DataTool」里的视频，自动收进素材库（你自己挑的，加分）
- 指纹去重：每个视频取 3 帧算指纹，画面一样的只留第一个；发过的永远不再发
"""
from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .runtime import ffmpeg_bin, log

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
INBOX_CHANNEL = "下载导入"


# ---------------------------------------------------------------- 小工具
def fmt_n(n: float) -> str:
    n = int(n)
    if n >= 10000:
        return f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(n)


def _bad_word(text: str, words: list[str]) -> str | None:
    t = (text or "").lower()
    for w in words or []:
        w = str(w).strip()
        if w and w.lower() in t:
            return w
    return None


def _reactions(m) -> int:
    r = getattr(m, "reactions", None)
    try:
        return sum(int(getattr(x, "count", 0) or 0) for x in (r.results or [])) if r else 0
    except Exception:  # noqa: BLE001
        return 0


def _short_side(m) -> int:
    f = getattr(m, "file", None)
    w, h = getattr(f, "width", None) or 0, getattr(f, "height", None) or 0
    return min(w, h) if w and h else 0


# ---------------------------------------------------------------- TG 打分
def channel_baseline(msgs) -> float | None:
    """这个群「平时」的浏览量：最近帖子浏览量的中位数（至少要 5 条有浏览量的）"""
    vs = [getattr(m, "views", None) for m in msgs]
    vs = [v for v in vs if v]
    return float(statistics.median(vs)) if len(vs) >= 5 else None


def dup_key(m) -> tuple | None:
    """同一个视频在不同群里转发：文件大小 + 时长一样就当成同一个"""
    f = getattr(m, "file", None)
    if not f or not getattr(f, "size", None):
        return None
    return (f.size, round(getattr(f, "duration", 0) or 0))


def score_tg(m, text: str, baseline: float | None, dup_n: int, rules: dict) -> dict:
    """返回 {score, reasons: [[类型, 说明]], out: 不合格（不下载）, weak: 不够火（这次先不下载）, ...}
    类型：g = 加分理由，b = 不合格的原因，"" = 普通信息"""
    reasons: list[list[str]] = []
    out = weak = False
    s = 50.0
    f = getattr(m, "file", None)
    dur = float(getattr(f, "duration", 0) or 0)
    if dur and dur < rules["min_sec"]:
        out = True
        reasons.append(["b", f"✗ 太短（{dur:.0f} 秒）"])
    if dur and dur > rules["max_sec"]:
        out = True
        reasons.append(["b", f"✗ {int(dur // 60)}:{int(dur % 60):02d} 超过 X 的 {int(rules['max_sec'] // 60)}:{int(rules['max_sec'] % 60):02d} 上限"])
    bad = _bad_word(text, rules.get("bad_words"))
    if bad:
        out = True
        reasons.append(["b", f"✗ 有广告词「{bad}」"])

    views = getattr(m, "views", None) or 0
    fw = getattr(m, "forwards", None) or 0
    rc = _reactions(m)
    ratio = None
    if views and baseline:
        ratio = views / baseline
        s += max(-30.0, min(40.0, 20 * math.log2(max(ratio, 0.01))))
        if ratio >= rules["min_ratio"]:
            reasons.append(["g", f"🔥 比这个群平时多 {ratio:.1f} 倍"])
        else:
            weak = True
            reasons.append(["", f"和这个群平时差不多（{ratio:.1f} 倍）" if ratio >= 0.8 else f"比这个群平时少（{ratio:.1f} 倍）"])
    elif views:
        reasons.append(["", f"浏览 {fmt_n(views)}（帖子太少，算不出平时多少）"])
    else:
        reasons.append(["", "这个群看不到浏览量"])

    d = getattr(m, "date", None)
    if d is not None and d.tzinfo is None:          # Telegram 给的是 UTC 时间；没带时区的当 UTC
        d = d.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - d).total_seconds() / 3600 if d else None
    if views and age_h is not None and age_h < 6 and ratio and ratio >= rules["min_ratio"]:
        s += 5
        reasons.append(["g", f"发了 {max(1, round(age_h))} 小时就 {fmt_n(views)} 浏览"])
    if fw:
        if views and fw / views >= 0.01:
            s += 8
            reasons.append(["g", f"转发 {fmt_n(fw)}（很多人转）"])
        else:
            reasons.append(["", f"转发 {fmt_n(fw)}"])
        if not views:
            s += min(15.0, fw / 20)
    if rc:
        if (views and rc / views >= 0.02) or (not views and rc >= 50):
            s += 5
            reasons.append(["g", f"表情回应 {fmt_n(rc)}"])
    if dup_n > 1:
        s += min(16, 8 * (dup_n - 1))
        reasons.append(["g", f"{dup_n} 个群都在发"])
    short = _short_side(m)
    if short and short < rules["min_short"]:
        s -= 15
        reasons.append(["", f"{dur:.0f} 秒 · {short}p，有点糊"])
    elif dur or short:
        reasons.append(["", f"{dur:.0f} 秒" + (f" · {short}p" if short else "")])
    s = max(0, min(100, round(s)))
    return {"score": 0 if out else s, "reasons": reasons, "out": out, "weak": weak, "views": views,
            "forwards": fw, "ratio": round(ratio, 2) if ratio else None, "source": "tg"}


# ---------------------------------------------------------------- 收件箱（智Tool 等插件下载的视频）
def parse_inbox_name(stem: str) -> dict:
    """「{id}__{resolution}__{title}」→ 编号、清晰度、标题；别的名字整个当标题"""
    parts = stem.split("__")
    if len(parts) >= 3:
        return {"id": parts[0].strip(), "res": parts[1].strip(), "title": "__".join(parts[2:]).strip()}
    if len(parts) == 2:
        return {"id": parts[0].strip(), "res": "", "title": parts[1].strip()}
    return {"id": "", "res": "", "title": stem.strip()}


def _res_short(res: str) -> int:
    m = re.search(r"(\d{3,4})p", res or "", re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", res or "")
    return min(int(m.group(1)), int(m.group(2))) if m else 0


def score_inbox(info: dict, dur: float, short: int, rules: dict) -> dict:
    reasons = [["g", "你自己挑的"]]
    out = False
    s = 72.0
    if dur and dur < rules["min_sec"]:
        out = True
        reasons.append(["b", f"✗ 太短（{dur:.0f} 秒）"])
    if dur and dur > rules["max_sec"]:
        out = True
        reasons.append(["b", f"✗ {int(dur // 60)}:{int(dur % 60):02d} 超过 X 的 {int(rules['max_sec'] // 60)}:{int(rules['max_sec'] % 60):02d} 上限"])
    bad = _bad_word(info.get("title", ""), rules.get("bad_words"))
    if bad:
        out = True
        reasons.append(["b", f"✗ 有广告词「{bad}」"])
    if short and short < rules["min_short"]:
        s -= 15
        reasons.append(["", f"{dur:.0f} 秒 · {short}p，有点糊"])
    else:
        reasons.append(["", f"{dur:.0f} 秒" + (f" · {short}p" if short else "")])
    return {"score": 0 if out else round(s), "reasons": reasons, "out": out, "weak": False, "source": "inbox"}


def collect_inbox(cfg: dict, dl_root: Path) -> int:
    """把收件箱（默认 ~/Downloads/DataTool）里下载完的视频搬进素材库的「下载导入/视频」，返回收了几个"""
    from .processor import probe

    ib = cfg.get("inbox") or {}
    if ib.get("on") is False:
        return 0
    src_dir = Path(str(ib.get("dir") or "~/Downloads/DataTool")).expanduser()
    if not src_dir.is_dir():
        return 0
    rules = pick_rules(cfg)
    dest = dl_root / INBOX_CHANNEL / "视频"
    dup_dir = src_dir / "已导入过的重复视频"
    n = 0
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS or dup_dir in f.parents:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if time.time() - st.st_mtime < 15:          # 可能还在下载 / 刚写完
            continue
        info = parse_inbox_name(f.stem)
        vid = re.sub(r"[^\w-]+", "_", info["id"])[:40] or f"f{abs(hash(f.stem)) % 10**10}"
        target = dest / f"{vid}{f.suffix.lower()}"
        if target.exists():                         # 同一个编号收过了
            dup_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dup_dir / f.name))
            log(f"  ⏭ 收件箱：{f.name} 收过了，挪到「已导入过的重复视频」")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(target))
        try:
            mi = probe(target)
            dur, short = mi.duration, min(mi.width, mi.height)
        except Exception:  # noqa: BLE001
            dur, short = 0.0, _res_short(info["res"])
        pick = score_inbox(info, dur, short or _res_short(info["res"]), rules)
        meta = {"channel": INBOX_CHANNEL, "channel_title": INBOX_CHANNEL, "id": vid, "text": info["title"], "link": "",
                "file": target.name, "kind": "video", "tags": [], "pick": pick, "imported": datetime.now().isoformat(timespec="seconds")}
        target.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    if n:
        log(f"📥 收件箱：收进 {n} 个视频（{src_dir}）")
    return n


def pick_rules(cfg: dict) -> dict:
    from .config import DEFAULTS

    r = {**DEFAULTS["pick"], **(cfg.get("pick") or {})}
    for k in ("min_ratio", "min_sec", "max_sec"):
        r[k] = float(r[k] or 0)
    r["min_short"] = int(r["min_short"] or 0)
    r["max_downloads"] = max(1, int(r["max_downloads"] or 15))
    if isinstance(r.get("bad_words"), str):
        r["bad_words"] = [w.strip() for w in re.split(r"[,，\s]+", r["bad_words"]) if w.strip()]
    return r


# ---------------------------------------------------------------- 指纹去重
def fingerprint(path: Path, duration: float) -> list[int] | None:
    """取 15% / 50% / 85% 三帧，缩成 9×8 灰度算 dHash（64 位）。画面一样、只是大小 / 清晰度 / 水印不同，指纹也接近"""
    out = []
    for pos in (0.15, 0.5, 0.85):
        t = max(0.0, duration * pos) if duration else 0.5
        try:
            r = subprocess.run([ffmpeg_bin("ffmpeg"), "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                                "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        px = r.stdout
        if len(px) < 72:
            return None
        h = 0
        for y in range(8):
            for x in range(8):
                h = (h << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
        out.append(h)
    return out


def similar(a: list[int], b: list[int], bits: int = 10) -> bool:
    """3 帧里至少 2 帧几乎一样"""
    if not a or not b or len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if bin(x ^ y).count("1") <= bits) >= 2


def find_same(state_data: dict, fp: list[int]) -> dict | None:
    for rec in state_data.get("fp", []):
        if similar(rec.get("h"), fp):
            return rec
    return None
