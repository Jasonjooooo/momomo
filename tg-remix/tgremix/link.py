"""给 Chrome 插件「X 时间工作流」用的本机小接口

只在本机 127.0.0.1:38719 上听，只认时间工作流插件（看浏览器自动带上的 Origin，网页伪造不了）：
  GET  /status  → 版本、是不是正在运行、在做什么、最近几行日志
  POST /run     → {"mode": "all" | "download" | "process"}  和点「下载并处理 / 只下载 / 只处理」一样
  POST /stop    → 和点「停止」一样
别的网页、别的插件都调不了；端口被占了就不开（不影响 TG二创 其他功能）。

只用 socket 和 threading（打包好的程序里一定有），不用 http.server：
小更新包只能换代码，换不了打包进去的 Python 标准库。
"""
from __future__ import annotations

import json
import socket
import threading

PORT = 38719
WORKFLOW_ID = "fpfdleplbfpeaajiplfdbokjcmefdmak"     # 时间工作流插件的 ID（它的 manifest 里有固定 key）
ALLOWED_ORIGINS = {f"chrome-extension://{WORKFLOW_ID}"}
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MODES = {"all", "download", "process"}
MAX_HEAD = 16 * 1024
MAX_BODY = 4096

_sock: socket.socket | None = None
_lock = threading.Lock()
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}


def _reply(conn: socket.socket, code: int, data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    head = (f"HTTP/1.1 {code} {REASONS.get(code, 'OK')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii")
    conn.sendall(head + body)


def _read_request(conn: socket.socket):
    """读一个请求：返回 (方法, 路径, 头, 正文)；格式不对返回 None"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > MAX_HEAD:
            return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, path, _ver = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    try:
        n = min(int(headers.get("content-length") or 0), MAX_BODY)
    except ValueError:
        n = 0
    while len(rest) < n:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    return method.upper(), path.split("?")[0], headers, rest[:n]


def handle(api, version: str, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
    """处理一个请求（单独拿出来，方便测试）"""
    if headers.get("host", "") not in ALLOWED_HOSTS:           # Host 不对 = 可能是 DNS 重绑定
        return 403, {"ok": False, "error": "forbidden"}
    origin = headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:   # 别的网页、别的插件
        return 403, {"ok": False, "error": "forbidden"}
    if method == "GET" and path == "/status":
        p = api.poll(max(0, len(api._lines) - 6))
        return 200, {"ok": True, "app": "TG二创", "version": version, "running": p["running"],
                     "status": p["status"], "progress": p["progress"], "lines": p["lines"][-6:]}
    if method != "POST":
        return (404 if path not in ("/run", "/stop") else 405), {"ok": False, "error": "not found"}
    if origin is None:                                          # 会改动东西的请求，必须是时间工作流插件发的
        return 403, {"ok": False, "error": "forbidden"}
    try:
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    if path == "/run":
        mode = data.get("mode") if data.get("mode") in MODES else "all"
        r = api.start(mode)
        if r.get("ok"):
            from .runtime import log
            log({"all": "⏰ 时间工作流到点了：开始下载并处理",
                 "download": "⏰ 时间工作流到点了：开始下载",
                 "process": "⏰ 时间工作流到点了：开始处理"}[mode])
        return 200, r
    if path == "/stop":
        return 200, api.stop()
    return 404, {"ok": False, "error": "not found"}


def _client(api, version: str, conn: socket.socket) -> None:
    try:
        conn.settimeout(5)
        req = _read_request(conn)
        if req is None:
            _reply(conn, 400, {"ok": False, "error": "bad request"})
            return
        code, data = handle(api, version, *req)
        _reply(conn, code, data)
    except Exception:  # noqa: BLE001  一个请求出错不影响别的
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _loop(api, version: str, sock: socket.socket) -> None:
    while True:
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        threading.Thread(target=_client, args=(api, version, conn), daemon=True).start()


def serve(api, version: str) -> bool:
    """开接口（只开一次）。端口被占了返回 False"""
    global _sock
    with _lock:
        if _sock is not None:
            return True
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # 刚重启时端口还在 TIME_WAIT 也能用
        try:
            s.bind(("127.0.0.1", PORT))
            s.listen(8)
        except OSError:
            s.close()
            return False
        _sock = s
        threading.Thread(target=_loop, args=(api, version, s), daemon=True, name="tgremix-link").start()
        return True
