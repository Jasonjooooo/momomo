"""
多连接分段下载：同一个文件开多条连接，同时下载不同部分。

Telethon 默认同一个数据中心（DC）的下载都走一条连接；如果这条连接被限速，
同时下再多文件也快不起来。这里给每个 DC 建一个连接池（默认 6 条），
所有文件的每一块（512KB）由空闲的连接去取。

任何异常（连接失败、CDN 重定向、大小不对……）都会抛出，由调用方改用普通下载。
思路参考 tulir 的 FastTelethon（MIT）。
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

PART = 512 * 1024  # Telegram 要求：能被 4KB 整除，且 1MB 能被它整除


class FastDownloadError(Exception):
    pass


class SenderPool:
    """某个 DC 的一组连接"""

    def __init__(self, client, dc_id: int, size: int):
        self.client = client
        self.dc_id = dc_id
        self.size = size
        self.queue: asyncio.Queue = asyncio.Queue()
        self.senders = []

    async def start(self) -> None:
        from telethon.network import MTProtoSender
        from telethon.tl import functions
        from telethon.tl.alltlobjects import LAYER

        c = self.client
        dc = await c._get_dc(self.dc_id)
        same_dc = self.dc_id == c.session.dc_id
        last = None
        # 一条一条建，中间歇一下（一下子开太多，Telegram / 代理会直接断开，报 IncompleteReadError）；
        # 建不上的跳过，有一条能用就行
        for i in range(self.size):
            sender = MTProtoSender(c.session.auth_key if same_dc else None, loggers=c._log)
            try:
                await asyncio.wait_for(sender.connect(c._connection(dc.ip_address, dc.port, dc.id, loggers=c._log,
                                                                    proxy=c._proxy, local_addr=c._local_addr)), 20)
                if not same_dc:  # 别的 DC 要导入登录授权
                    auth = await c(functions.auth.ExportAuthorizationRequest(self.dc_id))
                    c._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                    await asyncio.wait_for(sender.send(functions.InvokeWithLayerRequest(LAYER, c._init_request)), 20)
            except Exception as e:  # noqa: BLE001
                last = e
                try:
                    await sender.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1)
                continue
            self.senders.append(sender)
            self.queue.put_nowait(sender)
            if i + 1 < self.size:
                await asyncio.sleep(0.3)
        if not self.senders:
            raise FastDownloadError(f"一条连接都建不上（{type(last).__name__}）") from last

    async def get_part(self, location, offset: int) -> bytes:
        from telethon.tl import functions, types

        err = None
        for attempt in range(3):              # 取失败就换一条连接再试，最多 3 次
            if not self.senders:
                break
            sender = await self.queue.get()
            if sender is None:                # 连接全断了：叫醒排队的其他块，一起报错
                self.queue.put_nowait(None)
                break
            ok = False
            try:
                r = await asyncio.wait_for(sender.send(functions.upload.GetFileRequest(
                    location=location, offset=offset, limit=PART, precise=False, cdn_supported=False)), 60)
                ok = True
            except Exception as e:  # noqa: BLE001
                err = e
                if not sender.is_connected() and sender in self.senders:   # 断掉的连接不再用
                    self.senders.remove(sender)
                    try:
                        await sender.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    sender = None
                    if not self.senders:
                        self.queue.put_nowait(None)
                await asyncio.sleep(0.5 * (attempt + 1))
            finally:
                if sender is not None:
                    self.queue.put_nowait(sender)
            if ok:
                if isinstance(r, types.upload.FileCdnRedirect):
                    raise FastDownloadError("文件在 CDN 上，不支持多连接")
                return r.bytes
        raise FastDownloadError(f"这一块取了 3 次都失败（{type(err).__name__}）" if err else "连接全断了") from err

    async def close(self) -> None:
        for s in list(self.senders):
            try:
                await s.disconnect()
            except Exception:  # noqa: BLE001
                pass


class FastDownloader:
    """一次下载任务里共用的连接池"""

    def __init__(self, client, connections: int = 6):
        self.client = client
        self.connections = max(1, connections)
        self.pools: dict[int, SenderPool] = {}
        self._lock = asyncio.Lock()

    async def _pool(self, dc_id: int) -> SenderPool:
        async with self._lock:
            if dc_id not in self.pools:
                pool = SenderPool(self.client, dc_id, self.connections)
                self.pools[dc_id] = pool
                try:
                    await pool.start()
                except Exception:
                    await pool.close()
                    del self.pools[dc_id]
                    raise
            return self.pools[dc_id]

    async def download(self, document, path: Path, size: int, progress=None) -> None:
        from telethon import utils

        dc_id, location = utils.get_input_location(document)
        pool = await self._pool(dc_id)
        nparts = max(1, math.ceil(size / PART))
        got = 0
        with open(path, "wb") as f:
            f.truncate(size)

            async def one(i: int) -> None:
                nonlocal got
                data = await pool.get_part(location, i * PART)
                expect = min(PART, size - i * PART)
                if len(data) != expect:
                    raise FastDownloadError(f"第 {i + 1} 块大小不对（{len(data)} ≠ {expect}）")
                f.seek(i * PART)
                f.write(data)
                got += len(data)
                if progress:
                    progress(got, size)

            tasks = [asyncio.ensure_future(one(i)) for i in range(nparts)]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        if path.stat().st_size != size or got != size:
            raise FastDownloadError("下载的文件大小不对")

    async def close(self) -> None:
        for p in self.pools.values():
            await p.close()
        self.pools.clear()
