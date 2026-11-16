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


# ---------------------------------------------------------------- HTTPS 证书
_ssl_ctx = None


def ensure_ca_env() -> None:
    """打包后的应用找不到系统根证书，统一改用 certifi 自带的证书清单"""
    import os

    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass


def ssl_context():
    global _ssl_ctx
    if _ssl_ctx is None:
        import ssl

        try:
            import certifi

            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


# ---------------------------------------------------------------- 代理
# 常见代理软件的本地端口：(端口, 类型, 软件)
COMMON_PROXY_PORTS = [
    (7897, "socks5", "Clash Verge"), (7890, "socks5", "Clash / ClashX"), (7891, "socks5", "Clash"),
    (10808, "socks5", "v2rayN"), (1080, "socks5", "Shadowsocks"), (1087, "http", "V2rayU"),
    (6153, "socks5", "Surge"), (6152, "http", "Surge"), (20171, "socks5", "v2rayA"), (1086, "socks5", "ShadowsocksX"),
]


def system_proxy() -> dict | None:
    """读取 macOS 的系统代理设置（Clash 等开了「系统代理」时会写在这里）"""
    import subprocess
    import sys

    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    kv = {}
    for line in out.splitlines():
        if " : " in line:
            k, v = line.split(" : ", 1)
            kv[k.strip()] = v.strip()
    for kind, prefix in (("socks5", "SOCKS"), ("http", "HTTPS"), ("http", "HTTP")):
        if kv.get(f"{prefix}Enable") == "1" and kv.get(f"{prefix}Proxy") and kv.get(f"{prefix}Port"):
            return {"type": kind, "host": kv[f"{prefix}Proxy"], "port": int(kv[f"{prefix}Port"]), "from": "系统代理"}
    return None


def probe_local_proxy() -> dict | None:
    """探测本机是否有代理软件在常见端口上运行"""
    import socket

    for port, kind, name in COMMON_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return {"type": kind, "host": "127.0.0.1", "port": port, "from": f"自动探测（像是 {name}）"}
        except OSError:
            continue
    return None


def effective_proxy(tg: dict) -> dict | None:
    """
    实际使用的代理：
    - 手动填了地址 → 用手动的
    - 否则（自动模式）→ 系统代理 → 本机常见端口 → 不用代理
    """
    px = (tg or {}).get("proxy") or {}
    host = str(px.get("host") or "").strip()
    if host and px.get("port"):
        manual = {"type": px.get("type") or "socks5", "host": host, "port": int(px["port"]), "from": "手动设置"}
        # 手动填的是本机端口、但那个端口上没有代理在运行（比如填了 7890，Clash Verge 实际是 7897）→ 自动改用识别到的
        if host in ("127.0.0.1", "localhost") and not _port_open(host, manual["port"]):
            auto = system_proxy() or probe_local_proxy()
            if auto:
                auto["from"] = f"手动填的端口 {manual['port']} 没有代理在运行，改用{auto['from']}"
                return auto
        return manual
    if px.get("auto", True):
        return system_proxy() or probe_local_proxy()
    return None


def _port_open(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def proxy_url(px: dict | None) -> str | None:
    if not px:
        return None
    return f"{px.get('type') or 'socks5'}://{px['host']}:{int(px['port'])}"
