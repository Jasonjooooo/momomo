"""从 Telegram 频道下载视频（使用你自己的账号，通过官方 API）"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from .config import resolve
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


class _Progress:
    def __init__(self, label: str):
        self.label = label
        self.last = 0.0

    def __call__(self, done: int, total: int) -> None:
        now = time.time()
        if now - self.last < 0.5 and done < total:
            return
        self.last = now
        pct = done * 100 / total if total else 0
        print(f"\r    {self.label} {pct:5.1f}%  {done / 1048576:.1f}/{total / 1048576:.1f} MB", end="", flush=True)


async def _download(cfg: dict, state: State) -> int:
    from telethon import TelegramClient

    tg = cfg["telegram"]
    if not tg.get("api_id") or not tg.get("api_hash") or "换成" in str(tg.get("api_hash")):
        raise SystemExit("请先在 config.yaml 里填写 telegram.api_id 和 api_hash（在 https://my.telegram.org 申请）")
    if not cfg["channels"]:
        raise SystemExit("config.yaml 里还没有配置任何频道（channels）")

    dl_root = resolve(cfg, cfg["paths"]["downloads"])
    dl_root.mkdir(parents=True, exist_ok=True)
    rules = cfg["download"]
    total_new = 0

    client = TelegramClient(str(cfg["_root"] / tg["session"]), int(tg["api_id"]), str(tg["api_hash"]))
    await client.start()  # 第一次运行会在终端里要求输入手机号和验证码
    try:
        for ch in cfg["channels"]:
            raw = ch.get("channel") if isinstance(ch, dict) else ch
            opts = ch if isinstance(ch, dict) else {}
            try:
                entity = await client.get_entity(_normalize_channel(raw))
            except Exception as e:  # noqa: BLE001
                print(f"❌ 找不到频道 {raw}：{e}")
                continue

            username = getattr(entity, "username", None)
            key = username or str(entity.id)
            title = getattr(entity, "title", key)
            print(f"\n📺 {title}（{key}）")

            limit = int(opts.get("limit", 20))
            keywords = [k for k in (opts.get("keywords") or []) if k]
            exclude = [k for k in (opts.get("exclude") or []) if k]

            msgs = [m async for m in client.iter_messages(entity, limit=limit)]
            # 相册（多图/多视频）的文字只在其中一条消息上
            group_text = {m.grouped_id: m.message for m in msgs if m.grouped_id and m.message}

            ch_dir = dl_root / _safe_name(key)
            ch_dir.mkdir(exist_ok=True)
            for m in reversed(msgs):  # 从旧到新
                if not _is_video(m) or state.is_downloaded(key, m.id):
                    continue
                text = m.message or group_text.get(m.grouped_id, "") or ""
                dur = m.file.duration or 0
                size_mb = (m.file.size or 0) / 1048576
                reason = None
                if dur and dur < rules["min_duration"]:
                    reason = f"太短（{dur:.0f} 秒）"
                elif dur and dur > rules["max_duration"]:
                    reason = f"太长（{dur:.0f} 秒）"
                elif size_mb > rules["max_size_mb"]:
                    reason = f"太大（{size_mb:.0f} MB）"
                elif keywords and not any(k in text for k in keywords):
                    reason = "不含关键词"
                elif any(k in text for k in exclude):
                    reason = "含排除词"
                if reason:
                    print(f"  ⏭  #{m.id} 跳过：{reason}")
                    state.mark_downloaded(key, m.id)
                    continue

                ext = m.file.ext or ".mp4"
                final = ch_dir / f"{m.id}{ext}"
                part = ch_dir / f"{m.id}.part{ext}"
                print(f"  ⬇️  #{m.id}  {dur:.0f} 秒  {size_mb:.1f} MB")
                try:
                    await client.download_media(m, file=str(part), progress_callback=_Progress(f"#{m.id}"))
                    print()
                    part.replace(final)
                except Exception as e:  # noqa: BLE001
                    print(f"\n  ❌ #{m.id} 下载失败：{e}")
                    part.unlink(missing_ok=True)
                    continue

                link = f"https://t.me/{username}/{m.id}" if username else f"https://t.me/c/{entity.id}/{m.id}"
                meta = {
                    "channel": key,
                    "channel_title": title,
                    "id": m.id,
                    "date": m.date.isoformat() if m.date else "",
                    "text": text,
                    "link": link,
                    "file": final.name,
                }
                final.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                state.mark_downloaded(key, m.id)
                state.save()
                total_new += 1
            state.save()
    finally:
        await client.disconnect()
    return total_new


def download_all(cfg: dict, state: State) -> int:
    n = asyncio.run(_download(cfg, state))
    print(f"\n✅ 下载完成，新增 {n} 个视频")
    return n
