"""
transport/resp.py —— 最小 RESP（Redis Serialization Protocol）客户端。

为什么不用 `pip install redis`：本产品的硬承诺是**零硬依赖**（纯标准库）。
跨机分布式队列（路线 1）需要一个 Redis 客户端，于是手写一个够用的：
只覆盖本项目用到的命令面（LPUSH/RPOP/LLEN/HSET/HDEL/HGET/PING/AUTH/SELECT），
用标准库 socket 实现 RESP 2 的编解码。

支持五种回复类型（RESP2）：
  + 简单字符串 / - 错误 / : 整数 / $ 定长字符串（$-1 = nil）/ * 数组
连接时按 URL 自动 AUTH + SELECT（redis://[:password@]host:port/db）。  # scanner:allow —— 协议说明里的占位符，非真实凭证

这不是通用 Redis 客户端：没有连接池、没有发布订阅、没有集群、没有管道批量。
它的职责单一——让消息队列能跨机，且不引入任何第三方包。
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse


class RESPError(Exception):
    """Redis 返回的错误回复（- 开头）。"""


class RESPConnectionError(RESPError):
    """连接/读写失败。"""


class RESPClient:
    """线程不安全的极简 RESP 客户端（每个线程/进程各建一个，与 redis-py 习惯一致）。"""

    def __init__(self, url: str = "redis://localhost:6379/0",
                 timeout: float = 5.0, socket_timeout: float = 5.0):
        self.url = url
        self.timeout = timeout          # 连接超时
        self.socket_timeout = socket_timeout  # 读写超时
        self._sock: socket.socket | None = None
        self._buf = b""

    # ---- URL 解析：redis://[:password@]host:port/db ----
    def _parse(self):
        u = urlparse(self.url)
        if u.scheme not in ("redis", ""):
            raise RESPConnectionError(f"不支持的 URL scheme: {u.scheme}")
        host = u.hostname or "127.0.0.1"
        port = u.port or 6379
        db = 0
        if u.path and u.path.strip("/"):
            try:
                db = int(u.path.strip("/"))
            except ValueError:
                raise RESPConnectionError(f"无法解析 db 号: {u.path}")
        return host, port, (u.password or None), db

    # ---- 连接（含 AUTH/SELECT 握手）----
    def connect(self) -> "RESPClient":
        if self._sock is not None:
            return self
        host, port, password, db = self._parse()
        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
            sock.settimeout(self.socket_timeout)
        except OSError as e:
            raise RESPConnectionError(f"连接 Redis 失败 {host}:{port}: {e}") from e
        self._sock, self._buf = sock, b""
        try:
            if password:
                self.execute("AUTH", password)
            if db:
                self.execute("SELECT", db)
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # ---- 编码：*N\r\n $len\r\n arg\r\n ... ----
    @staticmethod
    def encode(args) -> bytes:
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            if isinstance(a, str):
                b = a.encode()
            elif isinstance(a, (bytes, bytearray)):
                b = bytes(a)
            else:
                # int/float 等：必须走 str()！bytes(3) 是三个零字节，不是 b"3"
                # （实测踩坑：connect() 里 SELECT db 传 int 被服务端拒）
                b = str(a).encode()
            parts.append(b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n")
        return b"".join(parts)

    # ---- 解码：按类型读一条完整回复 ----
    def _read_line(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RESPConnectionError("连接被 Redis 关闭")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n + 2:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RESPConnectionError("连接被 Redis 关闭")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n + 2:]
        return data

    def _read_reply(self):
        line = self._read_line()
        t, body = line[:1], line[1:]
        if t == b"+":
            return body.decode("utf-8", "replace")
        if t == b"-":
            raise RESPError(body.decode("utf-8", "replace"))
        if t == b":":
            return int(body)
        if t == b"$":
            n = int(body)
            if n == -1:
                return None
            return self._read_exact(n)
        if t == b"*":
            n = int(body)
            if n == -1:
                return None
            return [self._read_reply() for _ in range(n)]
        raise RESPError(f"未知 RESP 类型字节: {t!r}")

    # ---- 执行一条命令 ----
    def execute(self, *args):
        if self._sock is None:
            self.connect()
        self._sock.sendall(self.encode(args))
        return self._read_reply()

    # ---- 常用语义糖（统一解码为 str：队列里存的都是 JSON 文本）----
    @staticmethod
    def _s(v):
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v

    def ping(self) -> bool:
        try:
            return self._s(self.execute("PING")) == "PONG"
        except Exception:
            return False

    def lpush(self, key: str, value: str) -> int:
        return self.execute("LPUSH", key, value)

    def rpop(self, key: str):
        return self._s(self.execute("RPOP", key))

    def llen(self, key: str) -> int:
        return self.execute("LLEN", key)

    def hset(self, key: str, field: str, value: str) -> int:
        return self.execute("HSET", key, field, value)

    def hget(self, key: str, field: str):
        return self._s(self.execute("HGET", key, field))

    def hdel(self, key: str, *fields: str) -> int:
        return self.execute("HDEL", key, *fields)

    def hkeys(self, key: str) -> list:
        return [self._s(k) for k in (self.execute("HKEYS", key) or [])]

    def delete(self, *keys: str) -> int:
        return self.execute("DEL", *keys)
