"""图形界面的后台：界面里的按钮通过 window.pywebview.api.xxx() 调用这里的方法"""
from __future__ import annotations

import asyncio
import json
import re
import copy
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
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



POST_EXTS = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}   # 发帖页认的成品：视频和图片

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
        self._serve_link()
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

    def _serve_link(self) -> None:
        """开给 Chrome 插件「X 时间工作流」用的本机小接口（到点叫 TG二创 开始下载并处理）"""
        if getattr(self, "_link_tried", False):
            return
        self._link_tried = True
        from tgremix import link

        if link.serve(self, APP_VERSION, on_late=lambda: log("✅ 端口接上了，「X 时间工作流」插件现在能叫动 TG二创了")):
            return
        other = link.who_holds()
        if other:
            log(f"⚠️ 另一个 TG二创（{other}）还在后台开着，占着和「X 时间工作流」通信的端口。"
                "请把多出来的那个退掉（程序坞里右键 TG二创 → 退出，或活动监视器里结束多余的 TG二创），这边会自动接上")
        else:
            log(f"⚠️ 本机端口 {link.PORT} 被别的程序占了，「X 时间工作流」插件暂时叫不动 TG二创。"
                f"在终端运行 lsof -nP -iTCP:{link.PORT} -sTCP:LISTEN 可以看到是谁；它退出后这边会自动接上")

    def save_config(self, data: dict) -> dict:
        try:
            cfg = self._cfg()
            for k in ("telegram", "channels", "download", "process", "pick", "inbox", "caption"):
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
            from tgremix import picker

            picker.collect_inbox(cfg, folder(cfg, "downloads"))      # 智Tool 等插件下载的视频，先收进素材库
            if mode in ("all", "download") and not cfg["channels"]:
                log("ℹ️ 还没有添加 TG 群组，跳过 TG 下载")
            elif mode in ("all", "download"):
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
                state.save()
                self._prepare(cfg)
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

    # ------------------------------------------------------------ 发帖：挑成品、改文案、放进回关助手的发帖排期表
    def _out_root(self) -> Path:
        return folder(self._cfg(), "output")

    def _post_items(self) -> list[dict]:
        """最近 7 天的成品（有 .json 的，就是处理时记了来源和推荐分的）"""
        root = self._out_root()
        cut = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        items = []
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or d.name < cut or d.name == "试效果":
                continue
            for f in sorted(x for x in d.rglob("*") if x.suffix.lower() in POST_EXTS):
                mf = f.with_suffix(".json")
                if not mf.exists():
                    continue
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                items.append({"f": f, "meta": meta, "id": str(f.relative_to(root))})
        return items

    def _thumb(self, f: Path) -> str:
        import base64
        import hashlib

        from tgremix.runtime import ffmpeg_bin

        cache = self._root / ".thumbs"
        cache.mkdir(exist_ok=True)
        t = cache / (hashlib.md5(str(f).encode("utf-8")).hexdigest() + ".jpg")
        if not t.exists():
            subprocess.run([ffmpeg_bin("ffmpeg"), "-v", "error", "-y", *([] if f.suffix.lower() != ".mp4" else ["-ss", "1"]), "-i", str(f), "-frames:v", "1",
                            "-vf", "scale=360:-2", "-q:v", "5", str(t)], capture_output=True, timeout=60)
        return "data:image/jpeg;base64," + base64.b64encode(t.read_bytes()).decode() if t.exists() else ""

    @staticmethod
    def _caption(meta: dict) -> str:
        """文案草稿：有写好的就用写好的，没有就用原帖文字（去掉链接、@、话题标签、频道广告）"""
        if meta.get("caption") is not None:
            return meta["caption"]
        return Api._original(meta)

    # 原帖里频道自己加的推广行：有这些词的整行去掉
    PROMO = re.compile(r"频道|投稿|订阅|关注我|关注本|商务|合作|联系|广告|推广|加入|进群|交流群|官方群|点击|戳|👉|👇|➡|📣|📢|欢迎|爆料|私信")

    @staticmethod
    def _original(meta: dict) -> str:
        raw = str(meta.get("text") or meta.get("title") or "")
        if not raw.strip() and meta.get("src"):              # 以前处理的、没有帖子文字的：从原文件名里拿标题
            from tgremix.processor import title_from_name

            raw = title_from_name(str(meta["src"]).replace("\\", "/").split("/")[-1])
        lines = []
        for line in raw.splitlines():
            if Api.PROMO.search(line):
                continue
            t = re.sub(r"https?://\S+|t\.me/\S+|@\w+|[#＃]\S+", "", line)
            t = re.sub(r"\s+", " ", t).strip(" -_|·•【】[]")
            if len(t) >= 2:
                lines.append(t)
        return " ".join(lines)[:200]

    def post_use_original(self, ids: list | None = None) -> dict:
        """把 AI 写的文案换回原帖文字（ids 不给就是全部没排期的）"""
        n = 0
        want = set(ids) if ids else None
        for it in self._post_items():
            m = it["meta"]
            if m.get("post") and (m["post"].get("status") != "failed"):
                continue
            if want is not None and it["id"] not in want:
                continue
            m["caption"] = self._original(m)
            m.pop("caption_ai", None)
            try:
                it["f"].with_suffix(".json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
                n += 1
            except OSError:
                pass
        return {"ok": True, "count": n}

    def _ready(self) -> dict:
        try:
            return json.loads((self._root / "待发.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ids": [], "at": ""}

    def _prepare(self, cfg: dict | None = None) -> None:
        """挑出推荐分最高的 N 条（默认 40），没有 AI 文案的写好文案，记进「待发.json」，等你点「铺进排期表」"""
        from tgremix import caption as cap
        from tgremix.picker import pick_rules

        cfg = cfg or self._cfg()
        n = int(pick_rules(cfg).get("daily") or 40)
        items = [it for it in self._post_items() if not (it["meta"].get("pick") or {}).get("out") and not it["meta"].get("post")]
        items.sort(key=lambda it: -float((it["meta"].get("pick") or {}).get("score", 50)))
        chosen = items[:n]
        log(f"—— 准备发帖：可以发的 {len(items)} 条，挑推荐分最高的 {len(chosen)} 条 ——")
        c = cap.settings(cfg)
        if c.get("on") is not False:
            todo = [it for it in chosen if not it["meta"].get("caption_ai")]
            for i, it in enumerate(todo, 1):
                if stop_event.is_set():
                    raise Cancelled()
                self._set_status(f"AI 写文案 {i}/{len(todo)}…")
                self._caption_one(it, cfg)
        (self._root / "待发.json").write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                                          "ids": [it["id"] for it in chosen]}, ensure_ascii=False), encoding="utf-8")
        log(f"✅ 准备好 {len(chosen)} 条，去「发帖」页看一眼，点「铺进排期表」" + ("" if len(chosen) >= n else f"（不够 {n} 条，素材再多找一些）"))

    def _learned_path(self) -> Path:
        return self._root / "文案范例_你改过的.json"

    def _examples(self, cfg: dict) -> list[str]:
        """写文案时给 AI 看的范例：你在「AI 写文案」里填的 + 你在发帖页改过才发的（最近 15 条）"""
        typed = [x.strip(" -·•\t") for x in str((cfg.get("caption") or {}).get("examples") or "").splitlines()]
        try:
            learned = json.loads(self._learned_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            learned = []
        out = []
        for x in [t for t in typed if len(t) >= 4] + list(reversed(learned))[:15]:
            if x not in out:
                out.append(x)
        return out[:20]

    def _learn(self, meta: dict, text: str) -> None:
        """你把 AI 写的文案改了再发：改过的这句记下来当范例，下次照着你的口吻写"""
        ai = meta.get("caption_ai") or {}
        text = (text or "").strip()
        if not ai or len(text) < 4 or text == (ai.get("text") or "").strip() or text in (ai.get("alts") or []):
            return
        try:
            p = self._learned_path()
            try:
                lst = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                lst = []
            if text not in lst:
                lst = (lst + [text])[-30:]
                p.write_text(json.dumps(lst, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def post_use_alt(self, item_id: str, idx: int) -> dict:
        """换成另外一条 AI 写的文案（原来那条放回备选里）"""
        try:
            f = self._meta_path(item_id)
            mf = f.with_suffix(".json")
            m = json.loads(mf.read_text(encoding="utf-8"))
            ai = m.get("caption_ai") or {}
            alts = list(ai.get("alts") or [])
            if not 0 <= int(idx) < len(alts):
                return {"ok": False, "error": "没有这一条"}
            new = alts.pop(int(idx))
            if (ai.get("text") or "").strip():
                alts.insert(0, ai["text"])
            ai.update(text=new, alts=alts)
            m["caption"], m["caption_ai"] = new, ai
            mf.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True, "caption": new, "alts": alts}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, UserError) else _friendly(e)}

    def _caption_one(self, it: dict, cfg: dict) -> dict:
        from tgremix import caption as cap

        m, f = it["meta"], it["f"]
        if f.suffix.lower() != ".mp4":            # 图片：AI 文案只会看视频，图片先用原帖文字
            return {"ok": False, "error": "图片先不用 AI 写文案"}
        try:
            r = cap.write(f, float(m.get("duration") or 0), m.get("text") or m.get("title") or "", cfg, self._examples(cfg))
        except cap.CaptionError as e:
            log(f"    ⚠️ {f.name} 文案没写成：{e}（先用原帖文字）")
            return {"ok": False, "error": str(e)}
        m["caption"] = r["caption"]
        m["caption_ai"] = {"kind": r["kind"], "desc": r["desc"], "how": r["how"], "text": r["caption"], "alts": r.get("alts") or []}
        f.with_suffix(".json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"    ✍️ {f.name}【{r['kind'] or '?'}】{r['caption']}")
        return {"ok": True, **r}

    def post_prepare(self) -> dict:
        """重新挑今天的 N 条并写文案（在后台跑，运行页能看到进度）"""
        return self._start_thread(lambda: self._prepare())

    def post_recaption(self, item_id: str) -> dict:
        """这一条换一个文案"""
        try:
            f = self._meta_path(item_id)
            m = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
            m.pop("caption_ai", None)
            r = self._caption_one({"f": f, "meta": m}, self._cfg())
            return r if r.get("ok") else {"ok": False, "error": r.get("error")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, UserError) else _friendly(e)}

    def caption_check(self) -> dict:
        """AI 文案能不能用：本机 Ollama 和模型、DeepSeek key"""
        from tgremix import caption as cap

        c = cap.settings(self._cfg())
        ok, why = cap.ollama_ok(c)
        g_ok, g_why = cap.gemini_ok(c) if str(c.get("gemini_key") or "").strip() else (None, "")
        return {"ok": True, "gemini": g_ok, "gemini_msg": g_why or "Gemini 可以用（会用它看整条视频）",
                "ollama": ok, "ollama_msg": why or f"本机模型 {c['ollama_model']} 可以用",
                "deepseek": bool(str(c.get("api_key") or "").strip()), "model": c.get("model") or "deepseek-chat"}

    def post_list(self) -> dict:
        try:
            ready = set(self._ready().get("ids") or [])
            out = []
            for it in self._post_items():
                m, f = it["meta"], it["f"]
                p = m.get("pick") or {}
                out.append({"id": it["id"], "date": f.parent.name if f.parent.parent == self._out_root() else f.parent.parent.name,
                            "channel": m.get("channel_title", ""), "tags": m.get("tags") or [], "score": p.get("score", 50),
                            "out": bool(p.get("out")), "reasons": p.get("reasons") or [], "views": p.get("views") or 0,
                            "source": p.get("source", ""), "duration": m.get("duration", 0),
                            "kind": "photo" if f.suffix.lower() != ".mp4" else "video",
                            "size_mb": round(f.stat().st_size / 1048576, 1), "caption": self._caption(m), "post": m.get("post"),
                            "ready": it["id"] in ready, "ai": m.get("caption_ai") or None, "thumb": self._thumb(f)})
            out.sort(key=lambda x: (x["out"], bool(x["post"]), -x["score"]))
            seen = getattr(self, "_postq_seen", 0)
            note, note_at = getattr(self, "_postq_note", ("", 0))
            return {"ok": True, "items": out, "pump": {"seen_ago": int(time.time() - seen) if seen else -1,
                                                      "note": note if note and time.time() - note_at < 300 else ""}}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

    def _meta_path(self, item_id: str) -> Path:
        root = self._out_root().resolve()
        f = (root / item_id).resolve()
        if root not in f.parents or f.suffix.lower() not in POST_EXTS or not f.exists():
            raise UserError("找不到这个成品，可能被删了或者挪走了")
        return f

    def post_caption(self, item_id: str, text: str) -> dict:
        try:
            mf = self._meta_path(item_id).with_suffix(".json")
            meta = json.loads(mf.read_text(encoding="utf-8"))
            meta["caption"] = str(text or "")[:1000]
            mf.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, UserError) else _friendly(e)}

    def _queue_path(self) -> Path:
        return self._root / "发帖队列.json"

    def _queue(self) -> dict:
        try:
            return json.loads(self._queue_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"jobs": []}

    def _save_queue(self, q: dict) -> None:
        q["jobs"] = [j for j in q["jobs"] if j["status"] in ("waiting", "sending") or time.time() - j.get("at", 0) < 14 * 86400]
        tmp = self._queue_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._queue_path())

    def _set_post(self, f: Path, post: dict | None) -> None:
        mf = f.with_suffix(".json")
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
            meta["post"] = post
            mf.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _slots(self, day: str, n: int) -> list[tuple[str, str]]:
        """这一天 9:00–24:00 每 20 分钟一格（按选片规则），去掉已经过去的，随机留空，挑 n 格；不够就排到第二天"""
        import random

        from tgremix.picker import pick_rules

        r = pick_rules(self._cfg())
        toM = lambda t: int(str(t).split(":")[0]) * 60 + int(str(t).split(":")[1])  # noqa: E731
        start, end, step = toM(r.get("slot_start") or "09:00"), toM(r.get("slot_end") or "24:00"), max(5, int(r.get("slot_step") or 20))
        out, d = [], datetime.strptime(day, "%Y-%m-%d")
        for _ in range(10):
            key = d.strftime("%Y-%m-%d")
            now = datetime.now()
            grid = [m for m in range(start, end, step)
                    if not (key == now.strftime("%Y-%m-%d") and m < now.hour * 60 + now.minute + 10)]
            need = n - len(out)
            take = sorted(random.sample(grid, need)) if len(grid) > need else grid
            out += [(key, f"{m // 60:02d}:{m % 60:02d}") for m in take]
            if len(out) >= n:
                break
            d += timedelta(days=1)
        return out[:n]

    def post_queue(self, items: list, start_day: str, times: list | None = None) -> dict:
        """铺进排期表：从 start_day 开始，9:00–24:00 每 20 分钟一格随机挑（times 给了就按给的时间排）；
        时间工作流一分钟内来取，交给回关助手放进排期表，再自动交给 X 定时"""
        try:
            if not items:
                return {"ok": False, "error": "还没有选视频"}
            from tgremix.picker import pick_rules

            to_x = pick_rules(self._cfg()).get("to_x_auto") is True
            times = sorted({t for t in (times or []) if re.fullmatch(r"\d{2}:\d{2}", str(t))})
            day0 = datetime.strptime(start_day, "%Y-%m-%d")
            slots = None if times else self._slots(start_day, len(items))
            with self._lock:
                q = self._queue()
                busy = {j["file"] for j in q["jobs"] if j["status"] in ("waiting", "sending")}
                added = []
                for i, it in enumerate(items):
                    f = self._meta_path(it["id"])
                    if str(f) in busy:
                        continue
                    if slots is not None:
                        day, hm = slots[i] if i < len(slots) else slots[-1]
                    else:
                        day = (day0 + timedelta(days=i // len(times))).strftime("%Y-%m-%d")
                        hm = times[i % len(times)]
                    text = str(it.get("text") or "")[:1000]
                    qid = f"q{int(time.time() * 1000):x}{i}"
                    q["jobs"].append({"qid": qid, "file": str(f), "text": text, "day": day, "hm": hm, "status": "waiting", "at": time.time(),
                                      "to_x": to_x})
                    self._set_post(f, {"status": "waiting", "day": day, "hm": hm, "qid": qid})
                    meta_f = f.with_suffix(".json")
                    try:
                        meta = json.loads(meta_f.read_text(encoding="utf-8"))
                        self._learn(meta, text)
                        meta["caption"] = text
                        meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                    except (OSError, ValueError):
                        pass
                    added.append({"id": it["id"], "day": day, "hm": hm})
                self._save_queue(q)
            log(f"📤 {len(added)} 条放进发帖队列，等时间工作流取走交给回关助手（一分钟内）")
            return {"ok": True, "added": added}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, UserError) else _friendly(e)}

    def post_delete(self, ids: list) -> dict:
        """删掉没排期的成品：视频和 .json 挪进数据文件夹里的「已删除的成品」（7 天后自动清掉，想要回来可以去那里拿）。
        已经排进队列 / 放进排期表的不删（先取消排期）"""
        import hashlib
        import shutil

        bin_root = self._root / "已删除的成品"
        cut = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        if bin_root.exists():
            for d in bin_root.iterdir():
                if d.is_dir() and d.name < cut:
                    shutil.rmtree(d, ignore_errors=True)
        dest = bin_root / datetime.now().strftime("%Y-%m-%d")
        done, skipped, errors = [], 0, []
        for item_id in ids or []:
            try:
                f = self._meta_path(str(item_id))
                mf = f.with_suffix(".json")
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
                post = meta.get("post") or {}
                if post and post.get("status") != "failed":
                    skipped += 1
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                stem = f.stem
                while (dest / f"{stem}.mp4").exists():
                    stem += "_"
                shutil.move(str(f), str(dest / f"{stem}.mp4"))
                if mf.exists():
                    shutil.move(str(mf), str(dest / f"{stem}.json"))
                t = self._root / ".thumbs" / (hashlib.md5(str(f).encode("utf-8")).hexdigest() + ".jpg")
                t.unlink(missing_ok=True)
                done.append(str(item_id))
            except Exception as e:  # noqa: BLE001
                errors.append(str(e) if isinstance(e, UserError) else _friendly(e))
        if done:
            r = self._ready()
            if r.get("ids"):
                r["ids"] = [i for i in r["ids"] if i not in done]
                (self._root / "待发.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
            log(f"🗑 发帖页删了 {len(done)} 条成品（在「已删除的成品」里放 7 天）")
        return {"ok": not errors or bool(done), "deleted": len(done), "skipped": skipped, "error": "；".join(errors[:3])}

    def post_cancel(self, item_id: str) -> dict:
        try:
            f = self._meta_path(item_id)
            with self._lock:
                q = self._queue()
                for j in q["jobs"]:
                    if j["file"] == str(f) and j["status"] == "waiting":
                        j["status"] = "cancelled"
                        j["at"] = time.time()
                self._save_queue(q)
            self._set_post(f, None)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) if isinstance(e, UserError) else _friendly(e)}

    # 给时间工作流用（tgremix/link.py 调）：取队列、读视频、回报结果
    def _postq_waiting(self) -> list[dict]:
        with self._lock:
            q = self._queue()
            out = []
            for j in q["jobs"]:
                if j["status"] == "sending" and time.time() - j.get("sent_at", 0) > 600:
                    j["status"] = "waiting"          # 上次传到一半断了：重新来
                if j["status"] == "waiting" and Path(j["file"]).exists():
                    out.append({"qid": j["qid"], "name": Path(j["file"]).name, "size": Path(j["file"]).stat().st_size,
                                "type": POST_EXTS.get(Path(j["file"]).suffix.lower(), "video/mp4"), "text": j["text"], "day": j["day"], "hm": j["hm"],
                                "toX": j.get("to_x", False)})
            return out

    def _postq_file(self, qid: str) -> Path | None:
        with self._lock:
            q = self._queue()
            for j in q["jobs"]:
                if j["qid"] == qid and j["status"] in ("waiting", "sending"):
                    if j["status"] == "waiting":
                        j["status"] = "sending"
                        j["sent_at"] = time.time()
                        self._save_queue(q)
                    return Path(j["file"])
        return None

    def _postq_done(self, qid: str, ok: bool, day: str = "", hm: str = "", error: str = "") -> dict:
        with self._lock:
            q = self._queue()
            job = next((j for j in q["jobs"] if j["qid"] == qid), None)
            if not job:
                return {"ok": False, "error": "没有这个任务"}
            job["status"] = "done" if ok else "failed"
            job["at"] = time.time()
            if ok:
                job["day"], job["hm"] = day or job["day"], hm or job["hm"]
            else:
                job["error"] = error
            self._save_queue(q)
        f = Path(job["file"])
        self._set_post(f, {"status": "done", "day": job["day"], "hm": job["hm"]} if ok else {"status": "failed", "error": error})
        if ok:      # 发过的视频记下来，以后同样画面的不再处理
            st = State(self._root / "state.json")
            for rec in st.data.get("fp", []):
                if rec.get("out") == str(f):
                    rec["posted"] = True
            st.save()
            log(f"📅 已放进发帖排期表：{job['day']} {job['hm']} · {f.name}")
        else:
            log(f"⚠️ 没放进发帖排期表：{f.name}：{error}")
        return {"ok": True}

    def inbox_collect(self) -> dict:
        try:
            from tgremix import picker

            cfg = self._cfg()
            n = picker.collect_inbox(cfg, folder(cfg, "downloads"))
            return {"ok": True, "count": n, "dir": str(Path(str((cfg.get("inbox") or {}).get("dir") or "~/Downloads/DataTool")).expanduser())}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _friendly(e)}

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
