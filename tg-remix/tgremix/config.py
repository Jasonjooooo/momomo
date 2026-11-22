"""读取 config.yaml，缺的项用默认值补齐"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .runtime import UserError

DEFAULTS = {
    "telegram": {"api_id": 0, "api_hash": "", "session": "tg",
                 "proxy": {"auto": True, "type": "socks5", "host": "", "port": 0}},
    "channels": [],
    "download": {"min_duration": 5, "max_duration": 300, "max_size_mb": 500, "concurrency": 3, "connections": 6},
    # 选片：下载前给 TG 视频打分，只下载合适的（比这个群平时火 N 倍以上、时长、清晰度、没有广告词），每次最多下载 max_downloads 条
    "pick": {"on": True, "min_ratio": 2.0, "min_sec": 5, "max_sec": 140, "min_short": 720, "max_downloads": 80,
             "daily": 40, "slot_start": "09:00", "slot_end": "24:00", "slot_step": 20, "to_x_auto": False, "nsfw": True,
             "bad_words": ["加微信", "微信号", "vx", "私聊", "代理", "招商", "t.me/", "加群", "进群", "返利", "优惠券", "兼职", "博彩", "上车"],
             "times": ["12:00", "18:30", "21:30"]},
    # 发帖文案：本机 Ollama 看图模型描述画面 → DeepSeek 写（key 没填就用 AI 解说里的）
    "caption": {"on": True, "ollama_url": "http://127.0.0.1:11434", "ollama_model": "qwen2.5vl:7b",
                "api_base": "", "api_key": "", "model": "", "examples": "",
                "gemini_key": "", "gemini_model": "gemini-flash-latest"},
    # 收件箱：智Tool 等插件下载到这个文件夹的视频，自动收进素材库
    "inbox": {"on": True, "dir": "~/Downloads/DataTool"},
    "process": {
        "trim_head": 0,
        "trim_tail": 0,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        "blur_boxes": [],
        "speed": 1.1,
        "zoom": 1.0,
        "filter": "bright",
        "sharpen": True,
        "vignette": False,
        "layout": "vertical",
        "fps": 30,
        "title": {
            "enabled": True,
            "source": "post",
            "text": "",
            "max_chars_per_line": 14,
            "max_lines": 2,
            "font_size": 64,
            "color": "white",
            "box": True,
        },
        "watermark": {
            "image": "",
            "image_width": 0.18,
            "text": "",
            "font_size": 36,
            "opacity": 0.6,
            "position": "bottom_right",
        },
        "edit": {
            "enabled": False,
            "sensitivity": 0.7,
            "min_shot": 0.8,
            "drop_duplicates": True,
            "max_duration": 0,
            "transition": "fade",
            "transition_duration": 0.5,
        },
        "stickers": [],
        "texts": [],
        "voiceover": {
            "enabled": False,
            "api_base": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-chat",
            "style": "humor",
            "extra": "",
            "voice": "zh-CN-YunxiNeural",
            "rate": 1.1,
            "volume": 1.3,
            "duck_volume": 0.3,
            "subtitles": True,
        },
        "intro": "",
        "outro": "",
        "bgm": "",
        "bgm_volume": 0.25,
        "original_volume": 1.0,
        "font": "",
        "encoder": "auto",
        "video_bitrate": "8M",
    },
    "paths": {"downloads": "downloads", "output": "output"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"找不到配置文件 {path}\n"
            f"请先复制模板：cp config.example.yaml config.yaml，然后修改 config.yaml"
        )
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, data)
    cfg["_root"] = path.resolve().parent
    return cfg


def resolve(cfg: dict, p: str) -> Path:
    """相对路径以配置文件所在目录为准"""
    path = Path(p).expanduser()
    return path if path.is_absolute() else cfg["_root"] / path


FOLDER_NAMES = {"downloads": "下载文件夹", "output": "成品文件夹"}


def check_volume(p: Path, what: str = "文件夹") -> None:
    """文件夹在移动硬盘 / U 盘上（/Volumes/xxx）但没插：报错，免得偷偷建在电脑硬盘上"""
    parts = Path(p).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).is_dir():
        raise UserError(f"找不到磁盘「{parts[2]}」：{what}（{p}）在移动硬盘或 U 盘上，先插上再试；"
                        f"不用了的话，在「运行」页「文件夹」里改回来。")


def folder(cfg: dict, key: str) -> Path:
    """下载 / 成品文件夹：检查磁盘在不在，然后建好"""
    p = resolve(cfg, cfg["paths"][key])
    check_volume(p, FOLDER_NAMES.get(key, "文件夹"))
    p.mkdir(parents=True, exist_ok=True)
    return p
