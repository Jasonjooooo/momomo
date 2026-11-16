"""
筛选机器人：你把视频转发给机器人 → 规则 + AI 判断分类 → 合格的发进「素材库」频道，
20MB 以内的同时下载到 TG二创 的 downloads/机器人/ 里等待处理。

使用的是 Telegram 官方 Bot API（只需要 @BotFather 给的 token，不需要 my.telegram.org）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

from . import ai
from .config import resolve
from .runtime import effective_proxy, log, proxy_url, ssl_context

DEFAULT_CATEGORIES = ["搞笑动物", "搞笑", "轻微擦边", "新奇吃瓜"]
CATEGORY_HINTS = {
    "搞笑动物": "猫狗等动物的搞笑、萌宠、意外瞬间",
    "搞笑": "人物的搞笑段子、整活、翻车、沙雕日常",
    "轻微擦边": "成年人的性感、颜值、身材展示，但不露骨（没有裸露、没有性行为）",
    "新奇吃瓜": "新奇罕见的事、热点八卦、社会新闻现场、让人想围观的事件",
}
TG_FILE_LIMIT = 20 * 1024 * 1024  # Bot API 只能下载 20MB 以内的文件


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


class BotError(Exception):
    pass


class FilterBot:
    def __init__(self, root: Path, get_cfg):
        self.root = root
        self.get_cfg = get_cfg  # 每次读取最新配置，界面里改了立刻生效
        self.state_file = root / "bot_state.json"
        self.state = {"owner_id": None, "owner_name": "", "library_id": None, "library_title": "",
                      "offset": 0, "seen": [], "pending": {}, "stats": {}}
        if self.state_file.exists():
            try:
                self.state.update(json.loads(self.state_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.last_error = ""
        self.username = ""
        self._batch: dict = {"keep": 0, "drop": 0, "ask": 0, "cats": {}, "last": 0.0}

    # ------------------------------------------------------------ 状态
    def save(self) -> None:
        self.state["seen"] = self.state["seen"][-5000:]
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    def info(self) -> dict:
        return {
            "running": self.running, "username": self.username, "error": self.last_error,
            "owner": self.state.get("owner_name") or "", "library": self.state.get("library_title") or "",
            "stats": self.state.get("stats") or {}, "pending": len(self.state.get("pending") or {}),
        }

    # ------------------------------------------------------------ 启停
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="filter-bot")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.running = True
        try:
            asyncio.run(self._main())
        except BotError as e:
            self.last_error = str(e)
            log(f"🤖 ❌ {e}")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}：{e}"
            log(f"🤖 ❌ 机器人出错停止：{self.last_error}")
        finally:
            self.running = False
            log("🤖 机器人已停止")

    # ------------------------------------------------------------ Bot API
    async def _session(self):
        import aiohttp

        cfg = self.get_cfg()
        px = effective_proxy(cfg["telegram"])
        ctx = ssl_context()
        if px:
            from aiohttp_socks import ProxyConnector

            log(f"🤖 使用代理 {proxy_url(px)}（{px.get('from')}）")
            connector = ProxyConnector.from_url(proxy_url(px), ssl=ctx)
        else:
            connector = aiohttp.TCPConnector(ssl=ctx)
        return aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=70))

    async def call(self, method: str, **params):
        url = f"{self.api}/bot{self.token}/{method}"
        for attempt in range(5):
            try:
                async with self.http.post(url, json=params) as r:
                    data = await r.json(content_type=None)
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    raise BotError(f"连不上 Telegram（{type(e).__name__}）。请检查「账号 → 代理」设置和代理软件是否打开") from e
                await asyncio.sleep(2 + attempt * 2)
                continue
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            if code == 429:  # 发太快了，按要求等待
                await asyncio.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            if code == 401:
                raise BotError("机器人 Token 不对，请到 @BotFather 重新复制")
            raise BotError(f"{method} 失败：{data.get('description')}")
        raise BotError(f"{method} 多次重试仍失败")

    async def send(self, chat_id, text: str, **kw):
        return await self.call("sendMessage", chat_id=chat_id, text=text, **kw)

    # ------------------------------------------------------------ 主循环
    async def _main(self) -> None:
        cfg = self.get_cfg()
        bcfg = cfg.get("bot") or {}
        self.token = str(bcfg.get("token") or "").strip()
        if not re.fullmatch(r"\d+:[\w-]{20,}", self.token):
            raise BotError("请先填写正确的机器人 Token（@BotFather 给你的那一串，形如 123456:ABC-xyz…）")
        self.api = str(bcfg.get("api_base") or "https://api.telegram.org").rstrip("/")
        async with await self._session() as self.http:
            me = await self.call("getMe")
            self.username = me.get("username", "")
            log(f"🤖 机器人 @{self.username} 已启动，等待你转发视频…")
            await self.call("deleteWebhook", drop_pending_updates=False)
            while not self._stop.is_set():
                try:
                    updates = await self.call("getUpdates", offset=self.state["offset"], timeout=25,
                                              allowed_updates=["message", "channel_post", "callback_query"])
                except BotError as e:
                    if "Token" in str(e):
                        raise
                    log(f"🤖 ⚠️ {e}，10 秒后重试")
                    await asyncio.sleep(10)
                    continue
                for up in updates or []:
                    self.state["offset"] = up["update_id"] + 1
                    try:
                        await self._handle(up)
                    except BotError as e:
                        log(f"🤖 ⚠️ {e}")
                    except Exception as e:  # noqa: BLE001
                        log(f"🤖 ⚠️ 处理一条消息出错：{type(e).__name__}：{e}")
                    self.save()
                await self._maybe_summary()

    # ------------------------------------------------------------ 消息处理
    async def _handle(self, up: dict) -> None:
        if "callback_query" in up:
            return await self._on_button(up["callback_query"])
        if "channel_post" in up:
            post = up["channel_post"]
            if (post.get("text") or "").strip().startswith("/bind"):
                chat = post["chat"]
                self.state["library_id"] = chat["id"]
                self.state["library_title"] = chat.get("title", "")
                log(f"🤖 ✅ 已绑定素材库频道：{chat.get('title')}")
                await self.send(chat["id"], "✅ 已绑定为素材库，筛选合格的视频会发到这里。")
                if self.state.get("owner_id"):
                    await self.send(self.state["owner_id"], f"✅ 素材库已绑定：{chat.get('title')}")
            return
        msg = up.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        user = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        owner = self.state.get("owner_id")
        if owner is None and text.startswith("/start"):
            self.state["owner_id"] = user["id"]
            self.state["owner_name"] = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            log(f"🤖 主人已设置为：{self.state['owner_name']}")
            return await self.send(chat["id"], self._welcome())
        if owner != user.get("id"):
            return await self.send(chat["id"], "这是私人机器人，只为主人服务。")
        if text.startswith("/start") or text.startswith("/help"):
            return await self.send(chat["id"], self._welcome())
        if text.startswith("/stats"):
            return await self.send(chat["id"], self._stats_text())
        if self._video_of(msg):
            return await self._evaluate(msg)
        if text:
            await self.send(chat["id"], "把视频转发给我，我来帮你筛选。发送 /stats 查看统计。")

    def _welcome(self) -> str:
        lib = self.state.get("library_title")
        return (
            "👋 你好！我是你的视频筛选机器人。\n\n"
            "用法：在任何频道看到可能有用的视频，直接转发给我（可以一次多选转发）。"
            "我会按 TG二创 里设置的分类和规则自动筛选：\n"
            "✅ 合格 → 发到素材库，20MB 以内的同时下载进 TG二创\n"
            "❓ 拿不准 → 发按钮问你\n"
            "❌ 不合格 → 跳过\n\n"
            + (f"素材库：{lib}" if lib else
               "⚠️ 还没绑定素材库：新建一个私有频道，把我加为管理员，然后在频道里发一条 /bind")
        )

    def _stats_text(self) -> str:
        st = self.state.get("stats") or {}
        cats = "、".join(f"{k} {v}" for k, v in (st.get("cats") or {}).items()) or "暂无"
        return (f"📊 累计：收到 {st.get('total', 0)}，留下 {st.get('keep', 0)}，跳过 {st.get('drop', 0)}，"
                f"待确认 {len(self.state.get('pending') or {})}\n分类：{cats}")

    @staticmethod
    def _video_of(msg: dict) -> dict | None:
        if msg.get("video"):
            return msg["video"]
        doc = msg.get("document") or {}
        if (doc.get("mime_type") or "").startswith("video/"):
            return doc
        if msg.get("animation"):
            return msg["animation"]
        return None

    @staticmethod
    def _source(msg: dict) -> tuple[str, str]:
        """原始来源：(名称, 链接)"""
        o = msg.get("forward_origin") or {}
        chat = o.get("chat") or o.get("sender_chat") or msg.get("forward_from_chat") or {}
        if chat:
            uname = chat.get("username")
            mid = o.get("message_id") or msg.get("forward_from_message_id")
            link = f"https://t.me/{uname}/{mid}" if uname and mid else ""
            return chat.get("title") or uname or "频道", link
        u = o.get("sender_user") or msg.get("forward_from") or {}
        if u:
            return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "用户", ""
        if o.get("sender_user_name"):
            return o["sender_user_name"], ""
        return "", ""

    # ------------------------------------------------------------ 判断
    def _rules(self, v: dict, caption: str, bcfg: dict) -> str | None:
        dur = v.get("duration") or 0
        if dur and dur < float(bcfg.get("min_duration", 3)):
            return f"太短（{dur} 秒）"
        if dur and dur > float(bcfg.get("max_duration", 180)):
            return f"太长（{dur} 秒）"
        size_mb = (v.get("file_size") or 0) / 1048576
        if size_mb > float(bcfg.get("max_size_mb", 500)):
            return f"太大（{size_mb:.0f} MB）"
        w, h = v.get("width") or 0, v.get("height") or 0
        ori = bcfg.get("orientation", "any")
        if w and h and ori == "vertical" and w > h:
            return "不是竖屏"
        if w and h and ori == "horizontal" and h > w:
            return "不是横屏"
        low = caption.lower()
        for word in bcfg.get("exclude") or []:
            if word and word.lower() in low:
                return f"含排除词「{word}」"
        inc = [w for w in (bcfg.get("include") or []) if w]
        if inc and not any(w.lower() in low for w in inc):
            return "不含指定关键词"
        return None

    def _classify(self, caption: str, source: str, cats: list[str], acfg: dict) -> dict:
        lines = "\n".join(f"- {c}：{CATEGORY_HINTS.get(c, c)}" for c in cats)
        prompt = (
            "你是短视频素材筛选助手。根据视频的文字说明判断它属于哪一类。\n"
            f"可选分类：\n{lines}\n- 其他：都不属于\n\n"
            f"视频来源：{source or '未知'}\n视频文字说明：「{_clip(caption, 800)}」\n\n"
            "另外判断是否「不安全」：涉及未成年人的性暗示，或露骨色情（裸露、性行为），或血腥暴力。\n"
            '只回答 JSON：{"category": "分类名", "score": 0到100的把握度, "unsafe": true或false, "reason": "一句话理由"}'
        )
        return ai.chat_json(acfg, prompt, max_tokens=150, temperature=0.2, timeout=40)

    async def _evaluate(self, msg: dict) -> None:
        cfg = self.get_cfg()
        bcfg = cfg.get("bot") or {}
        acfg = cfg["process"].get("voiceover") or {}
        v = self._video_of(msg)
        caption = (msg.get("caption") or "").strip()
        source, link = self._source(msg)
        st = self.state.setdefault("stats", {})
        st["total"] = st.get("total", 0) + 1
        uid = v.get("file_unique_id") or f"{msg['chat']['id']}_{msg['message_id']}"
        item = {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"], "uid": uid, "caption": caption,
                "source": source, "link": link, "file_id": v.get("file_id"), "size": v.get("file_size") or 0,
                "category": ""}

        if uid in self.state["seen"]:
            return await self._drop(item, "重复的视频")
        reason = self._rules(v, caption, bcfg)
        if reason:
            return await self._drop(item, reason)

        cats = [c for c in (bcfg.get("categories") or []) if c]
        cats += [c.strip() for c in re.split(r"[,，、]", str(bcfg.get("custom_categories") or "")) if c.strip()]
        if not cats:
            return await self._keep(item)
        if not caption:
            mode = bcfg.get("no_caption", "ask")
            if mode == "keep":
                return await self._keep(item)
            if mode == "drop":
                return await self._drop(item, "没有文字，无法判断内容")
            return await self._ask(item, "没有文字说明，AI 看不出内容")
        if not str(acfg.get("api_key") or "").strip():
            return await self._ask(item, "没填 AI Key，无法自动分类")
        try:
            res = await asyncio.to_thread(self._classify, caption, source, cats, acfg)
        except ai.AIError as e:
            return await self._ask(item, f"AI 判断失败：{e}")
        cat = str(res.get("category") or "其他")
        score = int(res.get("score") or 0)
        why = str(res.get("reason") or "")
        if res.get("unsafe"):
            return await self._drop(item, f"不安全内容：{why}")
        threshold = int(bcfg.get("threshold", 60))
        if cat in cats and score >= threshold:
            item["category"] = cat
            return await self._keep(item, why)
        if cat in cats and score >= threshold - 25:
            item["category"] = cat
            return await self._ask(item, f"像是「{cat}」但不太确定（{score}%）：{why}")
        return await self._drop(item, f"分类为「{cat}」：{why}")

    # ------------------------------------------------------------ 三种结果
    async def _drop(self, item: dict, reason: str) -> None:
        st = self.state["stats"]
        st["drop"] = st.get("drop", 0) + 1
        self._batch["drop"] += 1
        self._batch["last"] = time.time()
        if reason != "重复的视频":
            self.state["seen"].append(item["uid"])
        log(f"🤖 ❌ 跳过：{reason}｜{_clip(item['caption'] or '（无文字）', 30)}")

    async def _ask(self, item: dict, reason: str) -> None:
        key = str(item["message_id"])
        self.state["pending"][key] = item
        self._batch["ask"] += 1
        self._batch["last"] = time.time()
        buttons = [[{"text": f"✅ 留下{'·' + item['category'] if item['category'] else ''}", "callback_data": f"k:{key}"},
                    {"text": "❌ 不要", "callback_data": f"d:{key}"}]]
        await self.call("sendMessage", chat_id=item["chat_id"], text=f"❓ {reason}\n这条要留下吗？",
                        reply_to_message_id=item["message_id"], reply_markup={"inline_keyboard": buttons})
        log(f"🤖 ❓ 待你确认：{reason}")

    async def _on_button(self, cq: dict) -> None:
        if (cq.get("from") or {}).get("id") != self.state.get("owner_id"):
            return await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="只有主人能操作")
        action, _, key = (cq.get("data") or "").partition(":")
        item = self.state["pending"].pop(key, None)
        if not item:
            return await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="这条已经处理过了")
        m = cq.get("message") or {}
        self._batch["ask"] = max(0, self._batch["ask"] - 1)  # 确认过的不再算「待确认」
        if action == "k":
            await self._keep(item, "你手动留下")
            note = "✅ 已留下"
        else:
            await self._drop(item, "你手动跳过")
            note = "❌ 已跳过"
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
        if m:
            await self.call("editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"], text=note)

    async def _keep(self, item: dict, why: str = "") -> None:
        cfg = self.get_cfg()
        bcfg = cfg.get("bot") or {}
        st = self.state["stats"]
        st["keep"] = st.get("keep", 0) + 1
        cat = item.get("category") or ""
        if cat:
            st.setdefault("cats", {})[cat] = st.get("cats", {}).get(cat, 0) + 1
            self._batch["cats"][cat] = self._batch["cats"].get(cat, 0) + 1
        self._batch["keep"] += 1
        self._batch["last"] = time.time()
        self.state["seen"].append(item["uid"])

        # 1) 发到素材库：保留原文，附上分类标签和来源
        lib = self.state.get("library_id")
        if lib:
            tail = " · ".join(x for x in (f"#{cat}" if cat else "", f"来源：{item['source']}" if item["source"] else "",
                                          item["link"]) if x)
            caption = (_clip(item["caption"], 900) + (f"\n\n{tail}" if tail else "")).strip()
            await self.call("copyMessage", chat_id=lib, from_chat_id=item["chat_id"],
                            message_id=item["message_id"], caption=caption)
            await asyncio.sleep(1.2)  # 频道发帖有频率限制
        # 2) 20MB 以内直接下载到 TG二创
        saved = ""
        if bcfg.get("auto_download", True) and item.get("file_id") and 0 < item["size"] <= TG_FILE_LIMIT:
            saved = await self._download(item, cfg)
        big = not saved and item["size"] > TG_FILE_LIMIT
        log(f"🤖 ✅ 留下{('「' + cat + '」') if cat else ''}{'，已下载' if saved else ''}{'（超过 20MB，需从素材库导出）' if big else ''}："
            f"{_clip(item['caption'] or '（无文字）', 30)}" + (f"（{why}）" if why else ""))

    async def _download(self, item: dict, cfg: dict) -> str:
        f = await self.call("getFile", file_id=item["file_id"])
        path = f.get("file_path")
        if not path:
            return ""
        out_dir = resolve(cfg, cfg["paths"]["downloads"]) / "机器人"
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(path).suffix or ".mp4"
        out = out_dir / f"{item['uid']}{ext}"
        async with self.http.get(f"{self.api}/file/bot{self.token}/{path}") as r:
            if r.status != 200:
                log(f"🤖 ⚠️ 下载失败（HTTP {r.status}）")
                return ""
            out.write_bytes(await r.read())
        meta = {"channel": "机器人", "channel_title": item["source"] or "机器人", "id": item["uid"],
                "text": item["caption"], "link": item["link"], "category": item.get("category", "")}
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(out)

    async def _maybe_summary(self) -> None:
        b = self._batch
        total = b["keep"] + b["drop"] + b["ask"]
        if not total or time.time() - b["last"] < 8 or not self.state.get("owner_id"):
            return
        cats = "、".join(f"{k} {v}" for k, v in b["cats"].items())
        text = f"📦 本批 {total} 条：留下 {b['keep']}" + (f"（{cats}）" if cats else "") + \
               f"，跳过 {b['drop']}" + (f"，待你确认 {b['ask']}" if b["ask"] else "")
        if not self.state.get("library_id") and b["keep"]:
            text += "\n⚠️ 还没绑定素材库，只下载了 20MB 以内的视频"
        self._batch = {"keep": 0, "drop": 0, "ask": 0, "cats": {}, "last": 0.0}
        await self.send(self.state["owner_id"], text)
