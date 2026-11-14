"""贴纸（图片 / GIF）和动态文字"""
from __future__ import annotations

from pathlib import Path

from .config import resolve


def _window(item: dict, out_dur: float) -> tuple[float, float]:
    """出现时间段：start 秒开始，持续 duration 秒（0 = 一直到结束）"""
    s = max(0.0, float(item.get("start") or 0))
    d = float(item.get("duration") or 0)
    e = out_dur if d <= 0 else min(out_dur, s + d)
    return min(s, max(0.0, out_dur - 0.1)), e


def _anchor(pos: str, W: int, H: int, margin: int, wexpr: str, hexpr: str) -> tuple[str, str]:
    """九宫格位置 → overlay 的 x、y 表达式"""
    x = {"l": f"{margin}", "c": f"({W}-{wexpr})/2", "r": f"{W}-{wexpr}-{margin}"}
    y = {"t": f"{int(H * 0.12)}", "m": f"({H}-{hexpr})/2", "b": f"{H}-{hexpr}-{int(H * 0.12)}"}
    table = {
        "top_left": ("l", "t"), "top": ("c", "t"), "top_right": ("r", "t"),
        "left": ("l", "m"), "center": ("c", "m"), "right": ("r", "m"),
        "bottom_left": ("l", "b"), "bottom": ("c", "b"), "bottom_right": ("r", "b"),
    }
    cx, cy = table.get(pos, ("r", "t"))
    return x[cx], y[cy]


def sticker_filters(cfg: dict, stickers: list, cur: str, n_in: int, W: int, H: int, out_dur: float,
                    tag: str = "st") -> tuple[list[str], list[str], str, int]:
    """返回 (新增的输入参数, 滤镜, 新的视频标签, 新的输入序号)"""
    inputs, graph = [], []
    margin = int(W * 0.04)
    for i, st in enumerate(stickers or []):
        if not st.get("image"):
            continue
        img = resolve(cfg, st["image"])
        if not img.exists():
            raise FileNotFoundError(f"贴纸图片不存在：{img}")
        s, e = _window(st, out_dur)
        if e - s < 0.2:
            continue
        if img.suffix.lower() == ".gif":
            inputs += ["-ignore_loop", "0", "-t", f"{out_dur:.3f}", "-i", str(img)]
        else:
            inputs += ["-loop", "1", "-t", f"{out_dur:.3f}", "-i", str(img)]
        sw = max(8, int(W * float(st.get("width", 0.25))) // 2 * 2)
        op = min(1.0, max(0.05, float(st.get("opacity", 1.0))))
        anim = st.get("animation", "fade")
        chain = [f"scale={sw}:-1", "format=rgba", f"colorchannelmixer=aa={op:.2f}"]
        if anim in ("fade", "float"):
            fd = min(0.4, (e - s) / 3)
            chain.append(f"fade=t=in:st={s:.3f}:d={fd:.3f}:alpha=1")
            if e < out_dur - 0.05:
                chain.append(f"fade=t=out:st={e - fd:.3f}:d={fd:.3f}:alpha=1")
        lbl = f"[{tag}{i}]"
        graph.append(f"[{n_in}:v]{','.join(chain)}{lbl}")
        x, y = _anchor(st.get("position", "top_right"), W, H, margin, "w", "h")
        if anim == "float":  # 上下轻轻浮动
            y = f"{y}+{int(H * 0.008)}*sin(2*PI*t/2)"
        out = f"[{tag}o{i}]"
        graph.append(f"{cur}{lbl}overlay=x='{x}':y='{y}':enable='between(t,{s:.3f},{e:.3f})':eof_action=pass{out}")
        cur = out
        n_in += 1
    return inputs, graph, cur, n_in


def _q(s: str) -> str:
    return "'" + s.replace("\\", "/").replace("'", r"'\''") + "'"


def text_filters(texts: list, cur: str, W: int, H: int, k: float, out_dur: float, font: str | None,
                 tmpdir: Path, tag: str = "tx") -> tuple[list[str], str]:
    """动态文字：fade 淡入淡出 / slide 从左滑入 / marquee 跑马灯 / typewriter 逐字出现 / none 不动"""
    graph = []
    items = [t for t in (texts or []) if str(t.get("text") or "").strip()]
    if not items:
        return graph, cur
    if not font:
        raise RuntimeError("找不到中文字体，无法添加动态文字")
    for i, tx in enumerate(items):
        s, e = _window(tx, out_dur)
        if e - s < 0.2:
            continue
        text = str(tx["text"]).strip()
        fs = max(14, int(int(tx.get("font_size", 52)) * k))
        color = str(tx.get("color") or "white")
        box = ":box=1:boxcolor=black@0.4:boxborderw=12" if tx.get("box", True) else ":borderw=3:bordercolor=black@0.7"
        pos = tx.get("position", "bottom")
        y = {"top": f"{int(H * 0.2)}", "center": "(h-text_h)/2", "bottom": f"h-text_h-{int(H * 0.12)}"}.get(pos, "(h-text_h)/2")
        anim = tx.get("animation", "fade")
        xc = "(w-text_w)/2"
        alpha = "1"
        fd = min(0.5, (e - s) / 3)
        if anim == "fade":
            alpha = (f"if(lt(t,{s + fd:.3f}),(t-{s:.3f})/{fd:.3f},"
                     f"if(gt(t,{e - fd:.3f}),({e:.3f}-t)/{fd:.3f},1))")
            x = xc
        elif anim == "slide":
            x = f"if(lt(t,{s + 0.6:.3f}),-text_w+(t-{s:.3f})/0.6*({xc}+text_w),{xc})"
        elif anim == "marquee":
            speed = max(60, int(W * 0.18))
            x = f"w-mod((t-{s:.3f})*{speed},w+text_w)"
        else:
            x = xc
        if anim == "typewriter":
            # 逐字出现：每个字用一层 drawtext，按时间依次显示
            step = min(0.12, max(0.04, (e - s) * 0.5 / max(1, len(text))))
            for j in range(1, len(text) + 1):
                tf = tmpdir / f"{tag}{i}_{j}.txt"
                tf.write_text(text[:j], encoding="utf-8")
                t0 = s + (j - 1) * step
                t1 = (s + j * step) if j < len(text) else e
                out = f"[{tag}{i}_{j}]"
                graph.append(f"{cur}drawtext=fontfile={_q(font)}:textfile={_q(str(tf))}:fontsize={fs}:fontcolor={color}"
                             f":x=(w-tw)/2:y={y}{box}:enable='between(t,{t0:.3f},{t1:.3f})'{out}")
                cur = out
            continue
        tf = tmpdir / f"{tag}{i}.txt"
        tf.write_text(text, encoding="utf-8")
        out = f"[{tag}o{i}]"
        graph.append(f"{cur}drawtext=fontfile={_q(font)}:textfile={_q(str(tf))}:fontsize={fs}:fontcolor={color}"
                     f":alpha='{alpha}':x='{x}':y='{y}'{box}:enable='between(t,{s:.3f},{e:.3f})'{out}")
        cur = out
    return graph, cur
