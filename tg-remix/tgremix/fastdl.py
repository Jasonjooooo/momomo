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
        for _ in range(self.size):
            sender = MTProtoSender(c.session.auth_key if same_dc else None, loggers=c._log)
            await sender.connect(c._connection(dc.ip_address, dc.port, dc.id, loggers=c._log,
                                               proxy=c._proxy, local_addr=c._local_addr))
            if not same_dc:  # 别的 DC 要导入登录授权
                auth = await c(functions.auth.ExportAuthorizationRequest(self.dc_id))
                c._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                await sender.send(functions.InvokeWithLayerRequest(LAYER, c._init_request))
            self.senders.append(sender)
            self.queue.put_nowait(sender)

    async def get_part(self, location, offset: int) -> bytes:
        from telethon.tl import functions, types

        sender = await self.queue.get()
        try:
            r = await sender.send(functions.upload.GetFileRequest(location=location, offset=offset, limit=PART,
                                                                  precise=False, cdn_supported=False))
        finally:
            self.queue.put_nowait(sender)
        if isinstance(r, types.upload.FileCdnRedirect):
            raise FastDownloadError("文件在 CDN 上，不支持多连接")
        return r.bytes

    async def close(self) -> None:
        for s in self.senders:
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
