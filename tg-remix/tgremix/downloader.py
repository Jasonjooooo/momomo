"""从 Telegram 频道下载视频（使用你自己的账号，通过官方 API）"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from .config import resolve
from .runtime import Cancelled, UserError, check_stop, log, progress
from .state import State


def _normalize_channel(raw) -> object:
    """把各种写法统一成 Telethon 能识别的形式"""
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if "/+" in s or "joinchat" in s:
        return s  # 私密邀请链接，原样交给 Telethon（需已加入）
    s = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/(s/)?", "", s)
    return s.lstrip("@").split("/")[0]


def _is_video(msg) -> bool:
    f = msg.file
    return bool(f and (f.mime_type or "").startswith("video/") and not msg.gif and not getattr(msg, "video_note", None))


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:60] or "channel"


def proxy_settings(tg: dict):
    """代理设置（手动 / 系统代理 / 自动探测）→ Telethon 的 proxy 参数"""
    from .runtime import effective_proxy

    px = effective_proxy(tg)
    if not px:
        return None
    ptype = str(px.get("type") or "socks5").lower()
    if ptype not in ("socks5", "socks4", "http"):
        ptype = "socks5"
    return (ptype, px["host"], int(px["port"]))


def make_client(cfg: dict):
    from telethon import TelegramClient

    tg = cfg["telegram"]
    if not tg.get("api_id") or not tg.get("api_hash") or "换成" in str(tg.get("api_hash")):
        raise UserError("请先填写 Telegram 的 api_id 和 api_hash（在 https://my.telegram.org 申请）")
    return TelegramClient(
        str(cfg["_root"] / tg["session"]), int(tg["api_id"]), str(tg["api_hash"]),
        proxy=proxy_settings(tg), connection_retries=3, retry_delay=2,
    )


class _Speed:
    """汇总所有并行下载的进度和速度"""

    def __init__(self, total: int):
        self.total = total
        self.finished = 0
        self.bytes = {}  # 每个文件已下载的字节数
        self.start = time.time()
        self.last = 0.0
        self.window = []  # (时间, 总字节)，用来算最近几秒的速度
        self.parallel = 1

    def cb(self, key):
        def _cb(done: int, size: int) -> None:
            check_stop()  # 点了停止就中断下载
            self.bytes[key] = done
            self.report()
        return _cb

    def report(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        got = sum(self.bytes.values())
        self.window.append((now, got))
        self.window = [w for w in self.window if now - w[0] <= 5]
        t0, b0 = self.window[0]
        speed = (got - b0) / (now - t0) / 1048576 if now > t0 else 0
        active = self.total - self.finished
        progress(f"    已完成 {self.finished}/{self.total} 个 · 正在下载 {min(active, self.parallel)} 个 · "
                 f"{speed:.1f} MB/s · 共 {got / 1048576:.0f} MB")


def _video_ok(m, rules, keywords, exclude, text) -> str | None:
    dur = m.file.duration or 0
    size_mb = (m.file.size or 0) / 1048576
    if dur and dur < rules["min_duration"]:
        return f"太短（{dur:.0f} 秒）"
    if dur and dur > rules["max_duration"]:
        return f"太长（{dur:.0f} 秒）"
    if size_mb > rules["max_size_mb"]:
        return f"太大（{size_mb:.0f} MB）"
    if keywords and not any(k in text for k in keywords):
        return "不含关键词"
    if any(k in text for k in exclude):
        return "含排除词"
    return None


async def download_with_client(client, cfg: dict, state: State) -> int:
    """
    用已经登录的 client 下载所有频道的新视频，返回新下载的数量。
    先把各频道要下载的视频列出来，再并行下载（默认同时 3 个，每次请求 512KB）。
    """
    if not cfg["channels"]:
        raise UserError("还没有添加任何频道")

    dl_root = resolve(cfg, cfg["paths"]["downloads"])
    dl_root.mkdir(parents=True, exist_ok=True)
    rules = cfg["download"]
    parallel = max(1, min(5, int(rules.get("concurrency") or 3)))

    # ---------- 第 1 步：列出要下载的视频 ----------
    todo = []
    for ch in cfg["channels"]:
        check_stop()
        raw = ch.get("channel") if isinstance(ch, dict) else ch
        opts = ch if isinstance(ch, dict) else {}
        if not raw:
            continue
        try:
            entity = await client.get_entity(_normalize_channel(raw))
        except Exception as e:  # noqa: BLE001
            log(f"❌ 找不到频道 {raw}：{e}")
            continue
        username = getattr(entity, "username", None)
        key = username or str(entity.id)
        title = getattr(entity, "title", key)
        limit = int(opts.get("limit") or 20)
        keywords = [k for k in (opts.get("keywords") or []) if k]
        exclude = [k for k in (opts.get("exclude") or []) if k]

        msgs = [m async for m in client.iter_messages(entity, limit=limit)]
        group_text = {m.grouped_id: m.message for m in msgs if m.grouped_id and m.message}
        ch_dir = dl_root / _safe_name(key)
        ch_dir.mkdir(exist_ok=True)
        n_new = n_skip = 0
        for m in reversed(msgs):  # 从旧到新
            if not _is_video(m) or state.is_downloaded(key, m.id):
                continue
            text = m.message or group_text.get(m.grouped_id, "") or ""
            reason = _video_ok(m, rules, keywords, exclude, text)
            if reason:
                log(f"  ⏭ {title} #{m.id} 跳过：{reason}")
                state.mark_downloaded(key, m.id)
                n_skip += 1
                continue
            link = f"https://t.me/{username}/{m.id}" if username else f"https://t.me/c/{entity.id}/{m.id}"
            todo.append({"m": m, "key": key, "title": title, "dir": ch_dir, "text": text, "link": link})
            n_new += 1
        log(f"📺 {title}（{key}）：{n_new} 个新视频" + (f"，跳过 {n_skip} 个" if n_skip else ""))
    state.save()
    if not todo:
        log("没有新视频")
        return 0

    # ---------- 第 2 步：并行下载 ----------
    total_mb = sum((t["m"].file.size or 0) for t in todo) / 1048576
    log(f"⬇️ 开始下载 {len(todo)} 个视频，共 {total_mb:.0f} MB，同时下载 {parallel} 个")
    sp = _Speed(len(todo))
    sp.parallel = parallel
    sem = asyncio.Semaphore(parallel)
    new_count = 0

    async def one(t) -> None:
        nonlocal new_count
        m = t["m"]
        async with sem:
            check_stop()
            ext = m.file.ext or ".mp4"
            final = t["dir"] / f"{m.id}{ext}"
            part = t["dir"] / f"{m.id}.part{ext}"
            started = time.time()
            try:
                if getattr(m, "document", None) is not None:
                    # 直接用 512KB 一块下载（默认只有 128KB，走代理时来回次数多、慢）
                    await client.download_file(m.document, file=str(part), part_size_kb=512,
                                               file_size=m.file.size, progress_callback=sp.cb((t["key"], m.id)))
                else:
                    await client.download_media(m, file=str(part), progress_callback=sp.cb((t["key"], m.id)))
                part.replace(final)
            except Cancelled:
                part.unlink(missing_ok=True)
                raise
            except Exception as e:  # noqa: BLE001
                log(f"  ❌ {t['title']} #{m.id} 下载失败：{type(e).__name__} {e}")
                part.unlink(missing_ok=True)
                sp.finished += 1
                return
            meta = {"channel": t["key"], "channel_title": t["title"], "id": m.id,
                    "date": m.date.isoformat() if m.date else "", "text": t["text"], "link": t["link"],
                    "file": final.name}
            final.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            state.mark_downloaded(t["key"], m.id)
            state.save()
            new_count += 1
            sp.finished += 1
            size = (m.file.size or 0) / 1048576
            secs = max(0.1, time.time() - started)
            log(f"  ✅ {t['title']} #{m.id}  {size:.1f} MB（{size / secs:.1f} MB/s）")
            sp.report(force=True)

    results = await asyncio.gather(*(one(t) for t in todo), return_exceptions=True)
    for r in results:
        if isinstance(r, Cancelled):
            raise r
    secs = max(0.1, time.time() - sp.start)
    got = sum(sp.bytes.values()) / 1048576
    log(f"📊 共下载 {got:.0f} MB，用时 {secs:.0f} 秒，平均 {got / secs:.1f} MB/s"
        + ("（已启用 cryptg 加速）" if _has_cryptg() else ""))
    return new_count


def _has_cryptg() -> bool:
    try:
        import cryptg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


async def _download(cfg: dict, state: State) -> int:
    client = make_client(cfg)
    await client.start()  # 第一次运行会在终端里要求输入手机号和验证码
    try:
        return await download_with_client(client, cfg, state)
    finally:
        await client.disconnect()


def download_all(cfg: dict, state: State) -> int:
    """命令行用：自己登录、下载、断开"""
    n = asyncio.run(_download(cfg, state))
    log(f"\n✅ 下载完成，新增 {n} 个视频")
    return n
