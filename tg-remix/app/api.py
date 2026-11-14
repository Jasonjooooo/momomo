"""图形界面的后台：界面里的按钮通过 window.pywebview.api.xxx() 调用这里的方法"""
from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import yaml

from tgremix import runtime
from tgremix.config import DEFAULTS, load_config, resolve
from tgremix.runtime import Cancelled, UserError, log, stop_event
from tgremix.state import State

APP_VERSION = "1.0.0"


def _open(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class _TG:
    """Telethon 跑在单独的线程和事件循环里，界面线程通过 run() 调用"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="telegram").start()
        self.client = None
        self.key = None
        self.phone = ""

    def run(self, coro, timeout: float | None = None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def ensure(self, cfg: dict):
        from tgremix.downloader import make_client, proxy_settings

        tg = cfg["telegram"]
        key = (str(tg.get("api_id")), str(tg.get("api_hash")), str(proxy_settings(tg)), str(cfg["_root"]))
        if self.client is not None and key != self.key:
            await self.client.disconnect()
            self.client = None
        if self.client is None:
            self.client = make_client(cfg)
            self.key = key
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def drop(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None
                self.key = None


def _friendly(e: BaseException) -> str:
    """把 Telegram / 网络错误翻译成中文"""
    name = type(e).__name__
    msg = str(e)
    table = {
        "PhoneNumberInvalidError": "手机号格式不对，请带国家码，例如 +8613800000000",
        "PhoneNumberBannedError": "这个手机号被 Telegram 封禁了",
        "PhoneCodeInvalidError": "验证码不对，请重新输入",
        "PhoneCodeExpiredError": "验证码已过期，请重新发送",
        "PasswordHashInvalidError": "两步验证密码不对",
        "ApiIdInvalidError": "api_id 或 api_hash 不对，请到 my.telegram.org 核对",
        "FloodWaitError": "操作太频繁，Telegram 要求等待一段时间后再试",
    }
    if name in table:
        extra = f"（需等待 {getattr(e, 'seconds', '')} 秒）" if name == "FloodWaitError" else ""
        return table[name] + extra
    if isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)) or "connect" in msg.lower():
        return "连不上 Telegram：请确认代理软件已经打开，并在「账号 → 代理」里填对地址和端口（Clash 一般是 127.0.0.1 和 7890）"
    if isinstance(e, (SystemExit, UserError)):
        return str(e)
    return f"{name}：{msg}"


class Api:
    def __init__(self, root: Path, ffmpeg_dir: Path | None):
        self._root = root
        self._ffmpeg_dir = ffmpeg_dir
        self._window = None
        self._cfg_path = root / "config.yaml"
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._progress = ""
        self._status = "空闲"
        self._running = False
        self._me = ""
        self._tg = _TG()
        root.mkdir(parents=True, exist_ok=True)
        for sub in ("downloads", "output", "assets"):
            (root / sub).mkdir(exist_ok=True)
        if not self._cfg_path.exists():
            self._write_cfg(copy.deepcopy(DEFAULTS))
        runtime.set_ffmpeg_dir(ffmpeg_dir)
        runtime.set_log_handler(self._on_log)
        (root / "logs").mkdir(exist_ok=True)
        self._logfile = root / "logs" / f"{datetime.now():%Y-%m-%d}.log"

    # ------------------------------------------------------------ 内部
    def _on_log(self, msg: str, is_progress: bool) -> None:
        with self._lock:
            if is_progress:
                self._progress = msg.strip()
                return
            self._progress = ""
            for line in str(msg).splitlines() or [""]:
                if line.strip():
                    self._lines.append(f"{datetime.now():%H:%M:%S}  {line}")
            del self._lines[:-2000]
        try:
            with open(self._logfile, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        except OSError:
            pass

    def _cfg(self) -> dict:
        return load_config(self._cfg_path)

    def _write_cfg(self, cfg: dict) -> None:
        data = {k: v for k, v in cfg.items() if not k.startswith("_")}
        tmp = self._cfg_path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self._cfg_path)

    def _start_thread(self, target, *args) -> dict:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "已经在运行了"}
            self._running = True
        stop_event.clear()
        threading.Thread(target=self._guard, args=(target, *args), daemon=True).start()
        return {"ok": True}

    def _guard(self, target, *args) -> None:
        try:
            target(*args)
        except Cancelled:
            log("⏹ 已停止")
        except (SystemExit, UserError) as e:
            log(f"❌ {e}")
        except Exception as e:  # noqa: BLE001
            log(f"❌ 出错：{_friendly(e)}")
            try:
                with open(self._logfile, "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
            except OSError:
                pass
        finally:
            with self._lock:
                self._running = False
                self._progress = ""
                self._status = "空闲"

    def _set_status(self, s: str) -> None:
        with self._lock:
            self._status = s

    # ------------------------------------------------------------ 配置
    def get_state(self) -> dict:
        from tgremix.processor import find_font

        cfg = self._cfg()
        session_file = self._root / f"{cfg['telegram']['session']}.session"
        return {
            "version": APP_VERSION,
            "root": str(self._root),
            "config": {k: v for k, v in cfg.items() if not k.startswith("_")},
            "has_session": session_file.exists(),
            "me": self._me,
            "font": find_font(cfg["process"].get("font", "")) or "",
            "ffmpeg": runtime.ffmpeg_bin("ffmpeg"),
        }

    def save_config(self, data: dict) -> dict:
        try:
            cfg = self._cfg()
            for k in ("telegram", "channels", "download", "process"):
                if k in data:
                    cfg[k] = data[k]
            self._write_cfg(cfg)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------ 登录
    def check_login(self) -> dict:
        try:
            cfg = self._cfg()
            if not cfg["telegram"].get("api_id"):
                return {"ok": True, "authorized": False}
            client = self._tg.run(self._tg.ensure(cfg), timeout=40)
            ok = self._tg.run(client.is_user_authorized(), timeout=20)
            if ok:
                me = self._tg.run(client.get_me(), timeout=20)
                self._me = " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or "")
            return {"ok": True, "authorized": bool(ok), "me": self._me}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def login_send_code(self, phone: str) -> dict:
        phone = (phone or "").replace(" ", "")
        if not phone.startswith("+"):
            return {"ok": False, "error": "手机号要带国家码，例如 +8613800000000"}
        try:
            cfg = self._cfg()
            client = self._tg.run(self._tg.ensure(cfg), timeout=40)
            self._tg.run(client.send_code_request(phone), timeout=40)
            self._tg.phone = phone
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def login_code(self, code: str) -> dict:
        from telethon.errors import SessionPasswordNeededError

        try:
            client = self._tg.client
            if client is None or not self._tg.phone:
                return {"ok": False, "error": "请先发送验证码"}
            try:
                self._tg.run(client.sign_in(phone=self._tg.phone, code=str(code).strip()), timeout=40)
            except SessionPasswordNeededError:
                return {"ok": True, "need_password": True}
            return self.check_login()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def login_password(self, password: str) -> dict:
        try:
            client = self._tg.client
            self._tg.run(client.sign_in(password=password), timeout=40)
            return self.check_login()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def logout(self) -> dict:
        try:
            if self._tg.client is not None:
                try:
                    self._tg.run(self._tg.client.log_out(), timeout=20)
                except Exception:  # noqa: BLE001
                    pass
            self._tg.run(self._tg.drop(), timeout=10)
            cfg = self._cfg()
            (self._root / f"{cfg['telegram']['session']}.session").unlink(missing_ok=True)
            self._me = ""
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------ 运行
    def start(self, mode: str = "all", redo: bool = False) -> dict:
        return self._start_thread(self._work, mode, bool(redo))

    def _work(self, mode: str, redo: bool) -> None:
        from tgremix.downloader import download_with_client
        from tgremix.processor import process_all

        cfg = self._cfg()
        state = State(self._root / "state.json")
        try:
            if mode in ("all", "download"):
                self._set_status("连接 Telegram…")
                log("—— 开始下载 ——")
                client = self._tg.run(self._tg.ensure(cfg), timeout=60)
                if not self._tg.run(client.is_user_authorized(), timeout=20):
                    log("❌ 还没有登录 Telegram，请先到「账号」页登录")
                    return
                self._set_status("下载中…")
                n = self._tg.run(download_with_client(client, cfg, state))
                log(f"✅ 下载完成，新增 {n} 个视频")
            if mode in ("all", "process"):
                self._set_status("处理中…")
                log("—— 开始处理 ——")
                process_all(cfg, state, redo=redo)
        finally:
            state.save()

    def test(self, path: str) -> dict:
        if not path:
            return {"ok": False, "error": "请先选择一个视频"}
        return self._start_thread(self._test, path)

    def _test(self, path: str) -> None:
        from tgremix.processor import process_all

        cfg = self._cfg()
        out_dir = resolve(cfg, cfg["paths"]["output"]) / "试效果"
        cfg["paths"]["output"] = str(out_dir)
        self._set_status("试效果：处理前 15 秒…")
        log(f"—— 试效果：{Path(path).name} ——")
        started = time.time()
        if process_all(cfg, State(self._root / "state.json"), only=Path(path), seconds=15):
            vids = sorted(out_dir.rglob("*.mp4"), key=lambda f: f.stat().st_mtime)
            if vids and vids[-1].stat().st_mtime >= started - 1:
                _open(vids[-1])

    def stop(self) -> dict:
        stop_event.set()
        self._set_status("正在停止…")
        return {"ok": True}

    def poll(self, since: int = 0) -> dict:
        with self._lock:
            since = max(0, min(int(since or 0), len(self._lines)))
            return {
                "lines": self._lines[since:],
                "next": len(self._lines),
                "running": self._running,
                "status": self._status,
                "progress": self._progress,
            }

    def clear_log(self) -> dict:
        with self._lock:
            self._lines.clear()
        return {"ok": True}

    # ------------------------------------------------------------ 文件
    def pick(self, kind: str) -> str:
        import webview

        if self._window is None:
            return ""
        types = {
            "video": ("视频 (*.mp4;*.mov;*.mkv;*.webm;*.m4v;*.avi)",),
            "image": ("图片 (*.png;*.jpg;*.jpeg;*.webp)",),
            "audio": ("音频 (*.mp3;*.m4a;*.aac;*.wav;*.flac;*.ogg)",),
            "font": ("字体 (*.ttf;*.ttc;*.otf)",),
        }
        dialog_ns = getattr(webview, "FileDialog", None)
        if kind == "folder":
            dtype = dialog_ns.FOLDER if dialog_ns else webview.FOLDER_DIALOG
            res = self._window.create_file_dialog(dtype)
        else:
            dtype = dialog_ns.OPEN if dialog_ns else webview.OPEN_DIALOG
            res = self._window.create_file_dialog(dtype, allow_multiple=False, file_types=types.get(kind, ()))
        if not res:
            return ""
        return str(res[0] if isinstance(res, (list, tuple)) else res)

    def open_folder(self, which: str) -> dict:
        cfg = self._cfg()
        target = {
            "output": resolve(cfg, cfg["paths"]["output"]),
            "downloads": resolve(cfg, cfg["paths"]["downloads"]),
            "logs": self._root / "logs",
        }.get(which, self._root)
        target.mkdir(parents=True, exist_ok=True)
        _open(target)
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        import webbrowser

        if url.startswith("https://"):
            webbrowser.open(url)
        return {"ok": True}
