"""运行时公共设施：日志输出、停止信号、ffmpeg 位置（命令行和图形界面共用）"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------- 日志
_handler: Optional[Callable[[str, bool], None]] = None


def set_log_handler(fn: Optional[Callable[[str, bool], None]]) -> None:
    """图形界面设置自己的日志接收函数；命令行默认 print"""
    global _handler
    _handler = fn


def log(msg: str = "") -> None:
    if _handler:
        _handler(msg, False)
    else:
        print(msg, flush=True)


def progress(msg: str) -> None:
    """会被下一条覆盖的进度信息（如下载百分比）"""
    if _handler:
        _handler(msg, True)
    else:
        print("\r" + msg, end="", flush=True)


# ---------------------------------------------------------------- 错误
class UserError(Exception):
    """需要用户去改设置的错误（缺账号、缺频道等），直接把文字展示给用户"""


# ---------------------------------------------------------------- 停止
class Cancelled(Exception):
    pass


stop_event = threading.Event()


def check_stop() -> None:
    if stop_event.is_set():
        raise Cancelled()


# ---------------------------------------------------------------- ffmpeg
_ff_dir: Optional[Path] = None


def set_ffmpeg_dir(path: Optional[Path]) -> None:
    global _ff_dir
    _ff_dir = path


def ffmpeg_bin(name: str) -> str:
    """优先用应用里自带的 ffmpeg/ffprobe，其次用系统里装的"""
    if _ff_dir and (_ff_dir / name).exists():
        return str(_ff_dir / name)
    found = shutil.which(name)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if Path(d, name).exists():
            return str(Path(d, name))
    return name
