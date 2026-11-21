"""从 Telegram 频道下载视频（使用你自己的账号，通过官方 API）"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from .config import folder, resolve
from . import picker
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


KIND_DIRS = {"video": "视频", "photo": "图片", "file": "文件", "audio": "音频"}


def _kind_of(m) -> str | None:
    """消息类型：video / photo / text / audio / file / None（其他）"""
    if _is_video(m):
        return "video"
    if getattr(m, "photo", None) is not None:
        return "photo"
    f = getattr(m, "file", None)
    if getattr(m, "document", None) is not None and f is not None and not getattr(m, "sticker", None):
        mime = (f.mime_type or "")
        if mime.startswith("audio/") or getattr(m, "voice", None):
            return "audio"
        if not mime.startswith("video/"):
            return "file"
    if (m.message or "").strip() and getattr(m, "media", None) is None:
        return "text"
    return None


def _text_ok(text: str, keywords, exclude) -> str | None:
    if keywords and not any(k in text for k in keywords):
        return "不含关键词"
    if any(k in text for k in exclude):
        return "含排除词"
    return None


def _append_text(path: Path, m, text: str, link: str, tags) -> None:
    """文字消息汇总到「文字.csv」（Excel 能直接打开）"""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["日期", "内容", "链接", "标签"])
        w.writerow([m.date.strftime("%Y-%m-%d %H:%M") if m.date else "", text, link, "、".join(tags)])


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

    dl_root = folder(cfg, "downloads")
    rules = cfg["download"]
    parallel = max(1, min(8, int(rules.get("concurrency") or 3)))

    # ---------- 第 1 步：列出要下载的内容（视频 / 图片 / 文字） ----------
    todo = []
    videos = []           # 选片：视频先打分，最后只下载分最高的
    seen_dup: dict = {}   # 同一个视频在几个群出现
    prules = picker.pick_rules(cfg)
    pick_on = prules.get("on") is not False
    dialogs_loaded = False
    for ch in cfg["channels"]:
        check_stop()
        raw = ch.get("channel") if isinstance(ch, dict) else ch
        opts = ch if isinstance(ch, dict) else {}
        if not raw or opts.get("enabled") is False:
            continue
        target = _normalize_channel(raw)
        try:
            entity = await client.get_entity(target)
        except Exception as e:  # noqa: BLE001
            if isinstance(target, int) and not dialogs_loaded:
                # 从「我的群组」选的是编号，找不到时先把对话列表读一遍（会缓存群组信息）再试
                dialogs_loaded = True
                await client.get_dialogs()
                try:
                    entity = await client.get_entity(target)
                except Exception as e2:  # noqa: BLE001
                    log(f"❌ 找不到 {opts.get('title') or raw}：{e2}")
                    continue
            else:
                log(f"❌ 找不到频道 {raw}：{e}")
                continue
        username = getattr(entity, "username", None)
        key = username or str(entity.id)
        title = getattr(entity, "title", None) or opts.get("title") or key
        limit = int(opts.get("limit") or 20)
        keywords = [k for k in (opts.get("keywords") or []) if k]
        exclude = [k for k in (opts.get("exclude") or []) if k]
        types = set(opts.get("types") or ["video"])
        tags = [t for t in (opts.get("tags") or []) if t]

        msgs = [m async for m in client.iter_messages(entity, limit=limit)]
        group_text = {m.grouped_id: m.message for m in msgs if m.grouped_id and m.message}
        baseline = picker.channel_baseline(msgs) if pick_on else None
        base = dl_root / (_safe_name(username) if username else f"{_safe_name(title)}_{entity.id}")
        cnt = {"video": 0, "photo": 0, "text": 0, "skip": 0}
        for m in reversed(msgs):  # 从旧到新
            kind = _kind_of(m)
            if kind not in types or state.is_downloaded(key, m.id):
                continue
            text = m.message or group_text.get(m.grouped_id, "") or ""
            reason = _video_ok(m, rules, keywords, exclude, text) if kind == "video" else _text_ok(text, keywords, exclude)
            if reason:
                log(f"  ⏭ {title} #{m.id} 跳过：{reason}")
                state.mark_downloaded(key, m.id)
                cnt["skip"] += 1
                continue
            link = f"https://t.me/{username}/{m.id}" if username else f"https://t.me/c/{entity.id}/{m.id}"
            if kind == "text":
                _append_text(base / "文字.csv", m, text, link, tags)
                state.mark_downloaded(key, m.id)
            elif kind == "video" and pick_on:
                sub = base / KIND_DIRS["video"]
                dk = picker.dup_key(m)
                if dk:
                    seen_dup.setdefault(dk, set()).add(key)
                videos.append({"m": m, "key": key, "title": title, "dir": sub, "text": text, "link": link,
                               "kind": kind, "tags": tags, "_baseline": baseline, "_dk": dk})
            else:
                sub = base / KIND_DIRS.get(kind, "其他")
                sub.mkdir(parents=True, exist_ok=True)
                todo.append({"m": m, "key": key, "title": title, "dir": sub, "text": text, "link": link,
                             "kind": kind, "tags": tags})
            cnt[kind] += 1
        parts = [f"{cnt['video']} 个视频" if "video" in types else "", f"{cnt['photo']} 张图片" if "photo" in types else "",
                 f"{cnt['text']} 条文字" if "text" in types else ""]
        log(f"📺 {title}" + (f"【{'、'.join(tags)}】" if tags else "") + "：新的 " + "、".join(x for x in parts if x)
            + (f"，跳过 {cnt['skip']} 条" if cnt["skip"] else ""))
    # ---------- 选片：打分，不合格的不下载，只下载分最高的几条 ----------
    if videos:
        used_dk = set()
        good, weak, out = [], 0, 0
        for v in videos:
            dn = len(seen_dup.get(v["_dk"], ())) if v["_dk"] else 1
            p = picker.score_tg(v["m"], v["text"], v.pop("_baseline"), dn, prules)
            v["pick"] = p
            if p["out"]:
                out += 1
                state.mark_downloaded(v["key"], v["m"].id)        # 不合格的以后也不要
                log(f"  ⏭ {v['title']} #{v['m'].id} 不要：{'；'.join(r[1].lstrip('✗ ') for r in p['reasons'] if r[0] == 'b')}")
            elif p["weak"]:
                weak += 1                                          # 不够火：先不下，以后火了还有机会
            else:
                good.append(v)
        good.sort(key=lambda v: -v["pick"]["score"])
        chosen = []
        for v in good:
            dk = v.pop("_dk")
            if dk and dk in used_dk:                               # 好几个群都在发的，只下载一份
                state.mark_downloaded(v["key"], v["m"].id)
                continue
            if dk:
                used_dk.add(dk)
            if len(chosen) < prules["max_downloads"]:
                chosen.append(v)
        for v in chosen:
            v["dir"].mkdir(parents=True, exist_ok=True)
        todo.extend(chosen)
        log(f"🎯 选片：新视频 {len(videos)} 条，不合格 {out} 条，不够火 {weak} 条（比群里平时火不到 {prules['min_ratio']:g} 倍，先不下）；"
            f"下载分最高的 {len(chosen)} 条" + (f"（最多 {prules['max_downloads']} 条，其余的下次再看）" if len(good) > len(chosen) else ""))
    state.save()
    if not todo:
        log("没有需要下载的新视频 / 图片")
        return 0

    return await download_todo(client, cfg, state, todo)


async def download_todo(client, cfg: dict, state: State, todo: list) -> int:
    """
    并行下载一批消息（视频 / 图片 / 文件 / 音频），自动抓取和「浏览」页共用。
    todo 里每项：{"m", "key", "title", "dir", "text", "link", "kind", "tags"}
    """
    rules = cfg["download"]
    parallel = max(1, min(8, int(rules.get("concurrency") or 3)))
    total_mb = sum(((t["m"].file.size if t["m"].file else 0) or 0) for t in todo) / 1048576
    conns = int(rules.get("connections", 6) or 0)
    fast = {"on": conns > 1, "fd": None}
    if fast["on"]:
        from .fastdl import FastDownloader

        fast["fd"] = FastDownloader(client, conns)
    log(f"⚙️ 加速：cryptg {'已启用 ✅' if _has_cryptg() else '未启用（解密较慢）'} · "
        + (f"多连接分段下载 {conns} 条连接" if fast["on"] else "多连接下载已关闭"))
    log(f"⬇️ 开始下载 {len(todo)} 个文件，共 {total_mb:.0f} MB，同时下载 {parallel} 个")
    sp = _Speed(len(todo))
    sp.parallel = parallel
    sem = asyncio.Semaphore(parallel)
    new_count = 0

    async def one(t) -> None:
        nonlocal new_count
        m = t["m"]
        async with sem:
            check_stop()
            ext = (m.file.ext if m.file else None) or (".jpg" if t["kind"] == "photo" else ".mp4")
            orig = getattr(m.file, "name", None) if m.file else None
            if t["kind"] in ("file", "audio") and orig:  # 文件和音频保留原文件名
                final = t["dir"] / f"{m.id}_{_safe_name(Path(orig).stem)}{Path(orig).suffix or ext}"
            else:
                final = t["dir"] / f"{m.id}{ext}"
            part = final.with_name(final.name + ".part")
            started = time.time()
            how = "普通"
            try:
                done = False
                if t["kind"] in ("photo", "file", "audio"):
                    await client.download_media(m, file=str(part), progress_callback=sp.cb((t["key"], m.id)))
                    done, how = True, {"photo": "图片", "file": "文件", "audio": "音频"}[t["kind"]]
                if not done and fast["on"] and getattr(m, "document", None) is not None and m.file.size:
                    try:
                        await fast["fd"].download(m.document, part, m.file.size, sp.cb((t["key"], m.id)))
                        done, how = True, "多连接"
                    except Cancelled:
                        raise
                    except Exception as e:  # noqa: BLE001  多连接不行就退回普通下载，本次不再尝试
                        if fast["on"]:
                            fast["on"] = False
                            log(f"  ⚠️ 多连接下载不可用（{type(e).__name__}：{str(e)[:80]}），改用普通方式")
                        part.unlink(missing_ok=True)
                        sp.bytes[(t["key"], m.id)] = 0
                if not done and getattr(m, "document", None) is not None:
                    # 普通方式：512KB 一块（默认只有 128KB，走代理时来回次数多、慢）
                    await client.download_file(m.document, file=str(part), part_size_kb=512,
                                               file_size=m.file.size, progress_callback=sp.cb((t["key"], m.id)))
                elif not done:
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
                    "file": final.name, "kind": t["kind"], "tags": t["tags"]}
            if t.get("pick"):
                meta["pick"] = t["pick"]
            final.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            t["final"] = str(final)
            state.mark_downloaded(t["key"], m.id)
            state.save()
            new_count += 1
            sp.finished += 1
            size = ((m.file.size if m.file else 0) or 0) / 1048576
            secs = max(0.1, time.time() - started)
            log(f"  ✅ {t['title']} #{m.id}  {size:.1f} MB（{size / secs:.1f} MB/s，{how}）")
            sp.report(force=True)

    try:
        results = await asyncio.gather(*(one(t) for t in todo), return_exceptions=True)
    finally:
        if fast["fd"]:
            await fast["fd"].close()
    for r in results:
        if isinstance(r, Cancelled):
            raise r
    secs = max(0.1, time.time() - sp.start)
    got = sum(sp.bytes.values()) / 1048576
    log(f"📊 共下载 {got:.0f} MB，用时 {secs:.0f} 秒，平均 {got / secs:.1f} MB/s")
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
