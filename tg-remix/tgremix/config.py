"""读取 config.yaml，缺的项用默认值补齐"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS = {
    "telegram": {"api_id": 0, "api_hash": "", "session": "tg",
                 "proxy": {"auto": True, "type": "socks5", "host": "", "port": 0}},
    "channels": [],
    "download": {"min_duration": 5, "max_duration": 300, "max_size_mb": 500, "concurrency": 3},
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
    "bot": {
        "token": "",
        "autostart": False,
        "categories": ["搞笑动物", "搞笑", "轻微擦边", "新奇吃瓜"],
        "custom_categories": "",
        "threshold": 60,
        "no_caption": "ask",
        "min_duration": 3,
        "max_duration": 180,
        "max_size_mb": 500,
        "orientation": "any",
        "include": [],
        "exclude": ["广告", "推广", "加群", "代理", "博彩"],
        "auto_download": True,
        "auto_process": True,   # 下载后自动二创
        "send_back": True,      # 二创完成后把成品发回 Telegram
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
