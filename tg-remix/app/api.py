"""图形界面的后台：界面里的按钮通过 window.pywebview.api.xxx() 调用这里的方法"""
from __future__ import annotations

import asyncio
import re
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
from tgremix.config import DEFAULTS, FOLDER_NAMES, check_volume, folder, load_config, resolve
from tgremix.runtime import Cancelled, UserError, log, stop_event
from tgremix.state import State

from tgremix import __version__ as APP_VERSION


def _open(path: Path) -> None:
    """用系统默认程序打开文件或文件夹；打不开也不影响主流程"""
    cmd = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    try:
        subprocess.Popen([cmd, str(path)])
    except OSError:
        log(f"（无法自动打开：{path}）")


class _TG:
    """Telethon 跑在单独的线程和事件循环里，界面线程通过 run() 调用"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="telegram").start()
        self.client = None
        self.key = None
        self.phone = ""
        self.code_sent_at = 0.0
        self.qr_state: dict = {"state": "idle"}
        self.qr_cancel = False

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

    async def qr_flow(self, client) -> None:
        """扫码登录：一直等用户扫码；二维码过期就自动换新的，最多等约 5 分钟"""
        from telethon.errors import SessionPasswordNeededError

        from tgremix.qr import to_svg

        try:
            qr = await client.qr_login()
            for _ in range(10):
                self.qr_state = {"state": "waiting", "svg": to_svg(qr.url, scale=6)}
                left = (qr.expires - __import__("datetime").datetime.now(tz=qr.expires.tzinfo)).total_seconds()
                try:
                    await qr.wait(timeout=max(5.0, left - 1))
                    self.qr_state = {"state": "done"}
                    return
                except asyncio.TimeoutError:
                    if self.qr_cancel:
                        self.qr_state = {"state": "idle"}
                        return
                    await qr.recreate()
            self.qr_state = {"state": "expired"}
        except SessionPasswordNeededError:
            self.qr_state = {"state": "need_password"}
        except Exception as e:  # noqa: BLE001
            self.qr_state = {"state": "error", "error": _friendly(e)}

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
        "SendCodeUnavailableError": "这个号码能用的验证码发送方式都用完了（多半是「发送验证码」点了好几次）。"
                                    "请等几个小时再试，或者改用上面的「扫码登录」，不需要验证码",
        "PhoneNumberUnoccupiedError": "这个手机号还没有注册 Telegram",
        "PhoneNumberFloodError": "这个号码请求验证码太频繁了，请过几个小时再试，或改用「扫码登录」",
        "AuthRestartError": "登录流程需要重新开始，请重新发送验证码或改用扫码登录",
        "PhoneCodeEmptyError": "请填写验证码",
        "AuthTokenExpiredError": "二维码过期了，请重新点「扫码登录」",
    }
    if name in table:
        extra = f"（需等待 {getattr(e, 'seconds', '')} 秒）" if name == "FloodWaitError" else ""
        return table[name] + extra
    tg_net = isinstance(e, (ConnectionError, asyncio.TimeoutError, TimeoutError)) or (
        "proxy" in name.lower() or "socks" in msg.lower() or "connection to telegram" in msg.lower())
    if tg_net:
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
        self._base_build = 0       # 由启动器设置
        self._update_state = {}    # 由启动器设置：是否在用小更新、是否回退过
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
            "update_active": bool(self._update_state.get("active")),
            "update_fallback": self._update_state.get("fallback", ""),
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
        if self._tg.phone == phone and time.time() - self._tg.code_sent_at < 600:
            return {"ok": True, "already": True}  # 10 分钟内不重复请求，避免把发码方式用完
        try:
            cfg = self._cfg()
            client = self._tg.run(self._tg.ensure(cfg), timeout=40)
            self._tg.run(client.send_code_request(phone), timeout=40)
            self._tg.phone = phone
            self._tg.code_sent_at = time.time()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def login_qr_start(self) -> dict:
        """开始扫码登录，返回二维码（SVG）"""
        try:
            cfg = self._cfg()
            client = self._tg.run(self._tg.ensure(cfg), timeout=40)
            if self._tg.run(client.is_user_authorized(), timeout=20):
                return {**self.check_login(), "state": "done"}
            self._tg.qr_cancel = False
            self._tg.qr_state = {"state": "starting"}
            asyncio.run_coroutine_threadsafe(self._tg.qr_flow(client), self._tg.loop)
            for _ in range(60):
                if self._tg.qr_state.get("state") != "starting":
                    break
                time.sleep(0.25)
            return {"ok": True, **self._tg.qr_state}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def login_qr_status(self) -> dict:
        st = dict(self._tg.qr_state)
        if st.get("state") == "done":
            return {**self.check_login(), "state": "done"}
        return {"ok": True, **st}

    def login_qr_cancel(self) -> dict:
        self._tg.qr_cancel = True
        return {"ok": True}

    def login_code(self, code: str) -> dict:
        from telethon.errors import SessionPasswordNeededError

        code = re.sub(r"[\s-]", "", str(code or ""))
        if not re.fullmatch(r"\d{5,6}", code):
            return {"ok": False, "error": "验证码应该是 5 位数字，在 Telegram App 里「Telegram」官方账号（蓝色认证）发来的消息里。"
                                          "my.telegram.org 网页的字母验证码不是这个"}

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

    # ------------------------------------------------------------ 我的群组
    def list_dialogs(self, include_all: bool = False) -> dict:
        """列出账号里的群组和频道（include_all=True 时也包括私聊和机器人）"""
        from telethon import utils

        try:
            cfg = self._cfg()
            client = self._tg.run(self._tg.ensure(cfg), timeout=40)
            if not self._tg.run(client.is_user_authorized(), timeout=20):
                return {"ok": False, "error": "请先在「账号」页登录 Telegram"}
            dialogs = self._tg.run(client.get_dialogs(limit=None), timeout=180)
            out = []
            for d in dialogs:
                if not (d.is_group or d.is_channel) and not include_all:
                    continue
                e = d.entity
                kind = "群组" if d.is_group else "频道" if d.is_channel else ("机器人" if getattr(e, "bot", False) else "私聊")
                out.append({
                    "id": str(utils.get_peer_id(e)),
                    "title": d.name or getattr(e, "title", "") or "",
                    "kind": kind,
                    "username": getattr(e, "username", None) or "",
                    "members": getattr(e, "participants_count", None) or 0,
                    "unread": d.unread_count or 0,
                })
            return {"ok": True, "dialogs": out}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def dialog_avatar(self, peer_id: str) -> str:
        """群组 / 频道的小头像（data URL），有缓存"""
        from tgremix.downloader import _normalize_channel

        cache = self.__dict__.setdefault("_avatars", {})
        if peer_id in cache:
            return cache[peer_id]

        async def get(client):
            ent = await client.get_entity(_normalize_channel(peer_id))
            b = await client.download_profile_photo(ent, file=bytes, download_big=False)
            import base64

            return "data:image/jpeg;base64," + base64.b64encode(b).decode() if b else ""

        try:
            client = self._tg.run(self._tg.ensure(self._cfg()), timeout=40)
            cache[peer_id] = self._tg.run(get(client), timeout=30)
        except Exception:  # noqa: BLE001
            cache[peer_id] = ""
        return cache[peer_id]

    # ------------------------------------------------------------ 浏览
    def browse_list(self, peer_id: str, kind: str = "all", query: str = "", offset_id: int = 0) -> dict:
        from tgremix import browse

        try:
            client = self._tg.run(self._tg.ensure(self._cfg()), timeout=40)
            state = State(self._root / "state.json")
            return {"ok": True, **self._tg.run(browse.list_items(client, peer_id, kind, query, int(offset_id or 0), state),
                                               timeout=180)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def browse_preview(self, peer_id: str, msg_id: int) -> dict:
        from tgremix import browse

        try:
            client = self._tg.run(self._tg.ensure(self._cfg()), timeout=40)
            return {"ok": True, **self._tg.run(browse.preview(client, peer_id, msg_id), timeout=120)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def browse_video_preview(self, peer_id: str, msg_id: int) -> dict:
        """视频前 8 秒预览（只下载开头一小段），有缓存"""
        from tgremix import browse

        cache = self.__dict__.setdefault("_vpv", {})
        key = f"{peer_id}:{msg_id}"
        if key in cache:
            return cache[key]
        try:
            client = self._tg.run(self._tg.ensure(self._cfg()), timeout=40)
            r = {"ok": True, **self._tg.run(browse.video_preview(client, peer_id, msg_id, self._root / ".preview"),
                                             timeout=180)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}
        if r.get("video"):
            if len(cache) > 20:  # 只缓存最近 20 个
                cache.pop(next(iter(cache)))
            cache[key] = r
        return r

    def browse_download(self, peer_id: str, ids: list, play: bool = False) -> dict:
        """下载选中的内容（后台进行，进度在「运行」页日志里）；play=True 时下完自动打开"""
        if not ids:
            return {"ok": False, "error": "没有要下载的内容"}
        return self._start_thread(self._browse_dl, peer_id, list(ids), bool(play))

    def _browse_dl(self, peer_id: str, ids: list, play: bool) -> None:
        from tgremix import browse
        from tgremix.downloader import download_todo

        cfg = self._cfg()
        state = State(self._root / "state.json")
        self._set_status("下载选中的内容…")
        client = self._tg.run(self._tg.ensure(cfg), timeout=60)
        todo, texts = self._tg.run(browse.build_todo(client, peer_id, ids, cfg,
                                                     folder(cfg, "downloads"), state), timeout=120)
        log(f"—— 浏览页下载：{len(todo)} 个文件" + (f"，{texts} 条文字已存入 文字.csv" if texts else "") + " ——")
        if todo:
            self._tg.run(download_todo(client, cfg, state, todo))
        state.save()
        done = [t for t in todo if t.get("final")]
        self._last_browse_done = {"peer": peer_id, "ids": [t["m"].id for t in done] + ([] if not texts else ids)}
        if play and done:
            _open(Path(done[0]["final"]))

    def browse_done(self) -> dict:
        """浏览页用：最近一次下载完成的消息编号（用来标记 ✓ 已下载）"""
        return self.__dict__.get("_last_browse_done") or {}

    def open_downloaded(self, peer_id: str) -> dict:
        """打开某个群的下载文件夹"""
        return self.open_folder("downloads")

    def preview_dialog(self, peer_id: str) -> dict:
        """看看某个群最近 100 条里有多少视频 / 图片 / 文字"""
        from tgremix.downloader import _kind_of, _normalize_channel

        async def count(client):
            ent = await client.get_entity(_normalize_channel(peer_id))
            c = {"video": 0, "photo": 0, "text": 0, "other": 0}
            samples = []
            async for m in client.iter_messages(ent, limit=100):
                k = _kind_of(m) or "other"
                c[k] += 1
                if k in ("video", "text") and m.message and len(samples) < 3:
                    samples.append(m.message.strip().replace("\n", " ")[:40])
            return {"counts": c, "samples": samples}

        try:
            client = self._tg.run(self._tg.ensure(self._cfg()), timeout=40)
            return {"ok": True, **self._tg.run(count(client), timeout=90)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

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
                log(f"✅ 下载完成，新增 {n} 个文件")
            if mode in ("all", "process"):
                self._set_status("处理中…")
                log("—— 开始处理 ——")
                process_all(cfg, state, redo=redo)
        finally:
            state.save()

    def test(self, path: str, text: str = "") -> dict:
        if not path:
            return {"ok": False, "error": "请先选择一个视频"}
        return self._start_thread(self._test, path, text or "")

    def _test(self, path: str, text: str = "") -> None:
        from tgremix.processor import process_all

        cfg = self._cfg()
        out_dir = folder(cfg, "output") / "试效果"
        cfg["paths"]["output"] = str(out_dir)
        self._set_status("试效果：处理前 15 秒…")
        log(f"—— 试效果：{Path(path).name} ——")
        started = time.time()
        if process_all(cfg, State(self._root / "state.json"), only=Path(path), seconds=15, text=text):
            vids = sorted(out_dir.rglob("*.mp4"), key=lambda f: f.stat().st_mtime)
            if vids and vids[-1].stat().st_mtime >= started - 1:
                _open(vids[-1])

    def test_ai(self, text: str = "") -> dict:
        """写一句解说词并配音，播放给用户听"""
        from tgremix import voiceover as vo

        cfg = self._cfg()
        vcfg = cfg["process"].get("voiceover") or {}
        sample = (text or "").strip() or "一只橘猫在超市里自己推着购物车逛街，店员都看呆了"
        try:
            words = vo.write_commentary(sample, 10, vcfg)
            out = self._root / "logs" / "测试配音.mp3"
            vo.synthesize(words, vcfg, out)
            _open(out)
            return {"ok": True, "text": words}
        except vo.VoiceoverError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    # ------------------------------------------------------------ 更新
    def check_update(self) -> dict:
        """从 GitHub 下载小更新包（几百 KB），比较版本"""
        from tgremix import updater
        from tgremix.runtime import effective_proxy

        cfg = self._cfg()
        incoming = updater.update_dir(self._root) / "incoming.zip"
        try:
            updater.download(incoming, effective_proxy(cfg["telegram"]))
            info = updater.read_package(incoming)
        except updater.UpdateError as e:
            return {"ok": False, "error": str(e)}
        latest = info.get("version", "0")
        res = {"ok": True, "current": APP_VERSION, "version": latest, "notes": info.get("notes", "")}
        if updater.vtuple(latest) <= updater.vtuple(APP_VERSION):
            incoming.unlink(missing_ok=True)
            return {**res, "available": False}
        if int(info.get("min_base", 0)) > int(self._base_build or 0):
            return {**res, "available": False, "need_full": True, "url": updater.FULL_URL}
        return {**res, "available": True}

    def install_update(self) -> dict:
        from tgremix import updater

        incoming = updater.update_dir(self._root) / "incoming.zip"
        if not incoming.exists():
            return {"ok": False, "error": "请先检查更新"}
        try:
            info = updater.install(incoming, self._root, int(self._base_build or 0))
        except updater.UpdateError as e:
            return {"ok": False, "error": str(e)}
        incoming.unlink(missing_ok=True)
        log(f"⬆️ 已安装更新 {info.get('version')}，重启 TG二创 后生效")
        return {"ok": True, "version": info.get("version")}

    def install_update_file(self) -> dict:
        """从文件安装小更新包（比如我直接发给你的 TGRemix-update.zip）"""
        from tgremix import updater

        p = self.pick("update")
        if not p:
            return {"ok": False, "cancel": True}
        try:
            info = updater.install(Path(p), self._root, int(self._base_build or 0))
        except updater.UpdateError as e:
            return {"ok": False, "error": str(e)}
        log(f"⬆️ 已安装更新 {info.get('version')}，重启 TG二创 后生效")
        return {"ok": True, "version": info.get("version")}

    def restart_app(self) -> dict:
        """重新打开 TG二创（让更新生效）"""
        import os

        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            bundle = Path(sys.executable).resolve().parents[2]
            subprocess.Popen(["/bin/sh", "-c", f'sleep 1; open -n "{bundle}"'])
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return {"ok": True}
        return {"ok": False, "error": "请手动关闭再打开 TG二创"}

    # ------------------------------------------------------------ 代理
    def detect_proxy(self) -> dict:
        from tgremix.runtime import effective_proxy, probe_local_proxy, proxy_url, system_proxy

        cfg = self._cfg()
        px = effective_proxy(cfg["telegram"])
        return {
            "using": proxy_url(px) or "", "from": (px or {}).get("from", ""),
            "system": proxy_url(system_proxy()) or "", "probe": proxy_url(probe_local_proxy()) or "",
        }

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
            "sticker": ("贴纸 (*.png;*.gif;*.webp;*.jpg;*.jpeg)",),
            "update": ("TG二创 更新包 (*.zip)",),
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

    # ------------------------------------------------------------ 下载 / 成品文件夹
    def _paths(self, cfg: dict) -> dict:
        out = {}
        for k in ("downloads", "output"):
            p = resolve(cfg, cfg["paths"][k])
            home = str(Path.home())
            show = "~" + str(p)[len(home):] if str(p).startswith(home + "/") else str(p)
            out[k] = {"path": str(p), "show": show, "default": p == resolve(cfg, DEFAULTS["paths"][k]),
                      "missing_disk": _missing_disk(p)}
        return out

    def get_paths(self) -> dict:
        return {"ok": True, "paths": self._paths(self._cfg())}

    def set_path(self, which: str, path: str) -> dict:
        """换下载 / 成品文件夹。path 为空 = 恢复默认（数据文件夹里的 downloads / output）"""
        if which not in FOLDER_NAMES:
            return {"ok": False, "error": "不认识的文件夹"}
        if self._running:
            return {"ok": False, "error": "正在下载或处理，等停下来再改。"}
        cfg = self._cfg()
        default = DEFAULTS["paths"][which]
        p = resolve(cfg, (path or "").strip() or default).resolve()
        other_key = "output" if which == "downloads" else "downloads"
        other = resolve(cfg, cfg["paths"][other_key]).resolve()
        name, other_name = FOLDER_NAMES[which], FOLDER_NAMES[other_key]
        if p == other or other in p.parents or p in other.parents:
            return {"ok": False, "error": f"{name}和{other_name}不能是同一个，也不能一个放在另一个里面"
                                          f"（不然做好的成品会被当成素材再处理一遍）。现在的{other_name}是：{other}"}
        if p == Path.home() or p == Path("/"):
            return {"ok": False, "error": "不能直接用整个用户文件夹，在里面新建一个文件夹再选。"}
        try:
            check_volume(p, name)
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".tgremix-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except UserError as e:
            return {"ok": False, "error": str(e)}
        except OSError:
            return {"ok": False, "error": f"这个文件夹没法写入（没有权限，或者是只读的磁盘），换一个吧：{p}"}
        old = resolve(cfg, cfg["paths"][which])
        cfg["paths"][which] = default if p == resolve(cfg, default).resolve() else str(p)
        self._write_cfg(cfg)
        log(f"📁 {name}改成：{p}")
        return {"ok": True, "paths": self._paths(cfg), "old": str(old), "changed": old.resolve() != p}

    def open_folder(self, which: str) -> dict:
        cfg = self._cfg()
        target = {
            "output": resolve(cfg, cfg["paths"]["output"]),
            "downloads": resolve(cfg, cfg["paths"]["downloads"]),
            "logs": self._root / "logs",
        }.get(which, self._root)
        if which in FOLDER_NAMES:
            try:
                check_volume(target, FOLDER_NAMES[which])
            except UserError as e:
                return {"ok": False, "error": str(e)}
        target.mkdir(parents=True, exist_ok=True)
        _open(target)
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        import webbrowser

        if url.startswith("https://"):
            webbrowser.open(url)
        return {"ok": True}


def _missing_disk(p: Path) -> bool:
    """文件夹在 /Volumes/xxx 上，但那个磁盘现在没插"""
    parts = Path(p).parts
    return len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes" and not Path("/Volumes", parts[2]).is_dir()
