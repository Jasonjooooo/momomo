"""
小更新：只更新代码和界面（几百 KB），不用重新下载 75MB 的完整安装包。

更新包 TGRemix-update.zip 的结构：
  version.json   {"version": "1.3.0", "min_base": 2, "notes": "更新说明"}
  code/tgremix/  功能代码
  code/api.py    界面后台
  code/ui/       界面

安装位置：<数据文件夹>/.update/current/
启动器（main.py）每次打开时，如果这里的版本更新、且和当前大包兼容，就用这里的代码。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from pathlib import Path

REPO = "Jasonjooooo/momomo"
UPDATE_URL = f"https://github.com/{REPO}/releases/download/tg-remix-latest/TGRemix-update.zip"
FULL_URL = f"https://github.com/{REPO}/releases/tag/tg-remix-latest"


class UpdateError(Exception):
    pass


def vtuple(v: str) -> tuple:
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def update_dir(data_root: Path) -> Path:
    return data_root / ".update"


def read_package(zip_path: Path) -> dict:
    """检查更新包是否完整，返回 version.json 的内容"""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            if "version.json" not in names:
                raise UpdateError("更新包里没有 version.json，不是 TG二创 的更新包")
            for need in ("code/tgremix/__init__.py", "code/api.py", "code/ui/index.html"):
                if need not in names:
                    raise UpdateError(f"更新包不完整，缺少 {need}")
            info = json.loads(z.read("version.json").decode("utf-8"))
    except zipfile.BadZipFile as e:
        raise UpdateError("更新包损坏，请重新下载") from e
    if not info.get("version"):
        raise UpdateError("更新包里没有版本号")
    return info


def install(zip_path: Path, data_root: Path, base_build: int) -> dict:
    """解压到 .update/current（先解压到临时目录，成功后再替换，避免装一半）"""
    info = read_package(zip_path)
    if int(info.get("min_base", 0)) > base_build:
        raise UpdateError("这个更新需要先下载新的完整安装包（加了新组件）")
    root = update_dir(data_root)
    root.mkdir(parents=True, exist_ok=True)
    staging, current, old = root / "staging", root / "current", root / "old"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            target = (staging / m.filename).resolve()
            if not str(target).startswith(str(staging.resolve())):
                raise UpdateError("更新包里有异常路径，已拒绝安装")
        z.extractall(staging)
    shutil.rmtree(old, ignore_errors=True)
    if current.exists():
        current.rename(old)
    staging.rename(current)
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(root / "broken", ignore_errors=True)
    return info


def uninstall(data_root: Path) -> None:
    """删除已安装的小更新，回到大包自带的版本"""
    shutil.rmtree(update_dir(data_root) / "current", ignore_errors=True)


async def _download(url: str, out: Path, proxy: dict | None) -> None:
    import aiohttp

    from .runtime import ssl_context

    ctx = ssl_context()
    if proxy and proxy.get("host") and proxy.get("port"):
        from aiohttp_socks import ProxyConnector

        from .runtime import proxy_url

        conn = ProxyConnector.from_url(proxy_url(proxy), ssl=ctx)
    else:
        conn = aiohttp.TCPConnector(ssl=ctx)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status == 404:
                raise UpdateError("服务器上还没有更新包")
            if r.status != 200:
                raise UpdateError(f"下载更新失败（HTTP {r.status}）")
            out.write_bytes(await r.read())


def download(out: Path, proxy: dict | None, url: str | None = None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(_download(url or UPDATE_URL, out, proxy))
    except UpdateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"连不上 GitHub 下载更新（{type(e).__name__}）。请确认代理软件已打开，"
                          f"并在「账号 → 代理」里填对地址和端口") from e
