"""「浏览」页：按类型列出群里的内容、缩略图、放大预览、选中下载"""
from __future__ import annotations

import asyncio
import base64

from .downloader import KIND_DIRS, _append_text, _kind_of, _normalize_channel, _safe_name

PAGE = 30


def _filter_for(kind: str):
    from telethon.tl import types as T

    return {
        "video": T.InputMessagesFilterVideo(),
        "photo": T.InputMessagesFilterPhotos(),
        "file": T.InputMessagesFilterDocument(),
        "audio": T.InputMessagesFilterMusic(),
    }.get(kind)


def _sizes(m):
    if getattr(m, "photo", None) is not None:
        return list(getattr(m.photo, "sizes", None) or [])
    doc = getattr(m, "document", None)
    return list(getattr(doc, "thumbs", None) or []) if doc is not None else []


def _pick(sizes, big: bool):
    """小图用 320 左右的（m），大图用最大的"""
    from telethon.tl import types as T

    real = [x for x in sizes if not isinstance(x, (T.PhotoStrippedSize, T.PhotoPathSize))]
    if not real:
        return sizes[0] if sizes else None
    if big:
        return real[-1]
    for t in ("m", "s", "x"):
        for x in real:
            if getattr(x, "type", "") == t:
                return x
    return real[0]


def _data_url(b: bytes | None) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b).decode() if b else ""


async def thumb(client, m, big: bool = False) -> str:
    sizes = _sizes(m)
    if not sizes:
        return ""
    try:
        if big and getattr(m, "photo", None) is not None:
            return _data_url(await client.download_media(m, file=bytes))  # 图片放大：直接下原图
        size = _pick(sizes, big)
        return _data_url(await client.download_media(m, file=bytes, thumb=size)) if size is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _entity_key(entity) -> str:
    return getattr(entity, "username", None) or str(entity.id)


def _item(m) -> dict | None:
    kind = _kind_of(m)
    if not kind:
        return None
    f = getattr(m, "file", None)
    text = (m.message or "").strip()
    name = (getattr(f, "name", None) if f else None) or (text.splitlines()[0][:80] if text else "")
    if not name:
        name = {"video": f"视频_{m.id}", "photo": f"图片_{m.id}", "audio": f"音频_{m.id}", "file": f"文件_{m.id}"}.get(kind, "")
    return {
        "id": m.id, "kind": kind, "name": name, "text": text[:500],
        "size": (getattr(f, "size", None) or 0) if f else 0,
        "duration": (getattr(f, "duration", None) or 0) if f else 0,
        "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
    }


async def list_items(client, peer, kind: str, query: str, offset_id: int, state) -> dict:
    entity = await client.get_entity(_normalize_channel(peer))
    key = _entity_key(entity)
    flt = _filter_for(kind)
    items, scanned, last = [], 0, 0
    async for m in client.iter_messages(entity, limit=400, offset_id=offset_id or 0,
                                        filter=flt, search=query or None):
        scanned += 1
        last = m.id
        it = _item(m)
        if it and (kind in ("all", "") or it["kind"] == kind):
            it["_m"] = m
            items.append(it)
        if len(items) >= PAGE:
            break
    # 缩略图并行取
    sem = asyncio.Semaphore(6)

    async def th(it):
        async with sem:
            it["thumb"] = await thumb(client, it.pop("_m"))
            it["downloaded"] = state.is_downloaded(key, it["id"])

    await asyncio.gather(*(th(it) for it in items))
    more = len(items) >= PAGE or scanned >= 400
    return {"items": items, "next_offset": last if more else 0, "title": getattr(entity, "title", None) or ""}


async def preview(client, peer, msg_id: int) -> dict:
    entity = await client.get_entity(_normalize_channel(peer))
    m = await client.get_messages(entity, ids=int(msg_id))
    if not m:
        return {"img": ""}
    return {"img": await thumb(client, m, big=True), "kind": _kind_of(m) or "", "text": (m.message or "")[:1000]}


async def build_todo(client, peer, ids: list, cfg: dict, dl_root, state) -> tuple[list, int]:
    """把选中的消息变成下载任务；文字直接写进 文字.csv。返回 (任务, 文字条数)"""
    entity = await client.get_entity(_normalize_channel(peer))
    username = getattr(entity, "username", None)
    key = _entity_key(entity)
    title = getattr(entity, "title", None) or " ".join(
        x for x in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if x) or key
    tags = []
    for ch in cfg.get("channels") or []:
        if str(ch.get("channel")) in (str(peer), username or "-"):
            tags = [t for t in (ch.get("tags") or []) if t]
    base = dl_root / (_safe_name(username) if username else f"{_safe_name(title)}_{entity.id}")
    msgs = await client.get_messages(entity, ids=[int(i) for i in ids])
    todo, texts = [], 0
    for m in msgs:
        if not m:
            continue
        kind = _kind_of(m)
        link = f"https://t.me/{username}/{m.id}" if username else f"https://t.me/c/{entity.id}/{m.id}"
        if kind == "text":
            _append_text(base / "文字.csv", m, m.message or "", link, tags)
            state.mark_downloaded(key, m.id)
            texts += 1
        elif kind in KIND_DIRS:
            sub = base / KIND_DIRS[kind]
            sub.mkdir(parents=True, exist_ok=True)
            todo.append({"m": m, "key": key, "title": title, "dir": sub, "text": m.message or "",
                         "link": link, "kind": kind, "tags": tags})
    state.save()
    return todo, texts
