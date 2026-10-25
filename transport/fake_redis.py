"""最小 RESP 服务器（仅用于测试 / 同机压测，非生产 Redis 替代）。

线程内 socket 服务器，逐连接顺序处理命令，只实现本项目用到的命令
（PING/AUTH/SELECT + 队列面 + 分布式协同面）。不需要真 Redis 也能验证
编解码、队列语义与跨机原语（锁/心跳/死信）行为。

覆盖命令：PING/AUTH/SELECT/LPUSH/RPOP/LLEN/HSET/HGET/HDEL/HKEYS/DEL +
分布式协同面（SET[ NX][ EX]/GET/EXPIRE/EXISTS/TTL/SADD/SREM/SMEMBERS/
HINCRBY/RPOPLPUSH/LRANGE/LREM/WATCH/UNWATCH/MULTI/DISCARD/EXEC）。
TTL 惰性过期（每次派发前清扫）；WATCH 靠"键版本号"实现——键被任何写
改动则 EXEC 作废，与真 Redis 语义一致。

⚠️ 诚实边界：这是**协议兼容的最小实现**，不是 Redis。生产跨机部署应接
官方 Redis（ClusterCoordinator 的开关纪律：SECTRUST_REDIS=1 不可达即抛错）。
本模块仅供测试与同机多进程压测用（见 tools/cluster_stress_test.py）。
"""

from __future__ import annotations

import math
import socket
import threading
import time


class FakeRedisServer:
    """最小 RESP 服务器：list + hash + string + set + TTL + WATCH/MULTI/EXEC，

    逐连接顺序处理命令。
    """

    def __init__(self, password: str = None, db: int = 0):
        self.password = password          # scanner:allow 属性名赋值，非硬编码凭据
        self.db = db
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expires: dict[str, float] = {}     # key -> unix 到期时间
        self.versions: dict[str, int] = {}      # 键版本：写一次 +1（WATCH 用）
        self.selected_db = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass

    # ---- TTL 与版本 ----
    def _sweep(self):
        now = time.time()
        for k, exp in list(self.expires.items()):
            if exp <= now:
                self.expires.pop(k, None)
                self.strings.pop(k, None)
                self.lists.pop(k, None)
                self.hashes.pop(k, None)
                self.sets.pop(k, None)

    def _touch(self, key: str):
        self.versions[key] = self.versions.get(key, 0) + 1

    def _exists(self, key: str) -> bool:
        self._sweep()
        return (key in self.strings or key in self.lists
                or key in self.hashes or key in self.sets)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""  # noqa: F841 —— 保留兼容占位（当前按行读，不依赖 buf）
        f = conn.makefile("rb")
        watched: dict[str, int] = {}   # WATCH 的键 -> 当时版本
        queued: list[list[str]] = []   # MULTI 里排队的命令
        in_multi = False
        try:
            while not self._stop.is_set():
                line = f.readline()
                if not line:
                    return
                if not line.startswith(b"*"):
                    continue  # 忽略 inline 命令
                n = int(line[1:].strip())
                args = []
                for _ in range(n):
                    hdr = f.readline()
                    ln = int(hdr[1:].strip())
                    args.append(f.read(ln + 2)[:-2])
                args = [a.decode("utf-8", "replace") for a in args]
                cmd = args[0].upper() if args else ""

                # ---- 事务控制命令（连接级状态，不碰数据）----
                if cmd == "WATCH":
                    for k in args[1:]:
                        watched[k] = self.versions.get(k, 0)
                    conn.sendall(b"+OK\r\n")
                    continue
                if cmd == "UNWATCH":
                    watched.clear()
                    conn.sendall(b"+OK\r\n")
                    continue
                if cmd == "MULTI":
                    in_multi, queued = True, []
                    conn.sendall(b"+OK\r\n")
                    continue
                if cmd == "DISCARD":
                    in_multi, queued, watched = False, [], {}
                    conn.sendall(b"+OK\r\n")
                    continue
                if cmd == "EXEC":
                    if not in_multi:
                        conn.sendall(b"-ERR EXEC without MULTI\r\n")
                        continue
                    aborted = any(self.versions.get(k, 0) != v
                                  for k, v in watched.items())
                    if aborted:
                        in_multi, queued, watched = False, [], {}
                        conn.sendall(b"*-1\r\n")   # 空数组 = 事务作废
                        continue
                    replies = [self._dispatch(a) for a in queued]
                    in_multi, queued, watched = False, [], {}
                    body = b"".join(replies)
                    conn.sendall(b"*%d\r\n" % len(replies) + body)
                    continue
                if in_multi:
                    queued.append(args)
                    conn.sendall(b"+QUEUED\r\n")
                    continue

                conn.sendall(self._dispatch(args))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _bulk(self, v) -> bytes:
        if v is None:
            return b"$-1\r\n"
        vb = v.encode() if isinstance(v, str) else v
        return b"$%d\r\n%s\r\n" % (len(vb), vb)

    def _dispatch(self, args) -> bytes:
        """执行一条命令并**返回**完整 RESP 回复字节（EXEC 要拼接用）。"""
        cmd = args[0].upper()
        if cmd == "PING":
            return b"+PONG\r\n"
        if cmd == "AUTH":
            if self.password and args[1] == self.password:
                return b"+OK\r\n"
            return b"-WRONGPASS invalid password\r\n"
        if cmd == "SELECT":
            if args[1] == str(self.db):
                self.selected_db = int(args[1])
                return b"+OK\r\n"
            return b"-ERR invalid DB index\r\n"
        # ---- string ----
        if cmd == "SET":
            key, val = args[1], args[2]
            rest = [a.upper() for a in args[3:]]
            if "NX" in rest and self._exists(key):
                return b"$-1\r\n"
            self._sweep()
            self.strings[key] = val
            self._touch(key)
            if "EX" in rest:
                self.expires[key] = time.time() + int(rest[rest.index("EX") + 1])
            else:
                self.expires.pop(key, None)
            return b"+OK\r\n"
        if cmd == "GET":
            self._sweep()
            return self._bulk(self.strings.get(args[1]))
        if cmd == "EXPIRE":
            self._sweep()
            if not self._exists(args[1]):
                return b":0\r\n"
            self.expires[args[1]] = time.time() + int(args[2])
            self._touch(args[1])
            return b":1\r\n"
        if cmd == "EXISTS":
            return b":%d\r\n" % (1 if self._exists(args[1]) else 0)
        if cmd == "TTL":
            self._sweep()
            if not self._exists(args[1]):
                return b":-2\r\n"
            exp = self.expires.get(args[1])
            if exp is None:
                return b":-1\r\n"
            return b":%d\r\n" % max(1, math.ceil(exp - time.time()))
        if cmd == "DEL":
            n = 0
            for k in args[1:]:
                existed = False
                for store in (self.strings, self.lists, self.hashes, self.sets):
                    if store.pop(k, None) is not None:
                        existed = True
                self.expires.pop(k, None)
                if existed:
                    n += 1
                self._touch(k)
            return b":%d\r\n" % n
        # ---- list ----
        if cmd == "LPUSH":
            self._sweep()
            lst = self.lists.setdefault(args[1], [])
            for v in args[2:]:
                lst.insert(0, v)
            self._touch(args[1])
            return b":%d\r\n" % len(lst)
        if cmd == "RPOP":
            self._sweep()
            lst = self.lists.get(args[1]) or []
            if not lst:
                return b"$-1\r\n"
            v = lst.pop()
            self._touch(args[1])
            return self._bulk(v)
        if cmd == "RPOPLPUSH":
            self._sweep()
            src = self.lists.get(args[1]) or []
            if not src:
                return b"$-1\r\n"
            v = src.pop()
            self.lists.setdefault(args[2], []).insert(0, v)
            self._touch(args[1])
            self._touch(args[2])
            return self._bulk(v)
        if cmd == "LLEN":
            self._sweep()
            return b":%d\r\n" % len(self.lists.get(args[1]) or [])
        if cmd == "LRANGE":
            self._sweep()
            lst = self.lists.get(args[1]) or []
            start, stop = int(args[2]), int(args[3])
            if stop == -1:
                stop = len(lst) - 1
            start = max(0, start)
            stop = min(stop, len(lst) - 1)
            items = lst[start:stop + 1] if start <= stop else []
            out = [b"*%d\r\n" % len(items)]
            out += [self._bulk(v) for v in items]
            return b"".join(out)
        if cmd == "LREM":
            self._sweep()
            lst = self.lists.get(args[1]) or []
            count, value = int(args[2]), args[3]
            removed = 0
            if count >= 0:
                keep = []
                for v in lst:
                    if v == value and (count == 0 or removed < count):
                        removed += 1
                        continue
                    keep.append(v)
                self.lists[args[1]] = keep
            if removed:
                self._touch(args[1])
            return b":%d\r\n" % removed
        # ---- hash ----
        if cmd == "HSET":
            self._sweep()
            h = self.hashes.setdefault(args[1], {})
            new = 0 if args[2] in h else 1
            h[args[2]] = args[3]
            self._touch(args[1])
            return b":%d\r\n" % new
        if cmd == "HGET":
            self._sweep()
            h = self.hashes.get(args[1]) or {}
            return self._bulk(h.get(args[2]))
        if cmd == "HDEL":
            self._sweep()
            h = self.hashes.get(args[1]) or {}
            n = sum(1 for k in args[2:] if h.pop(k, None) is not None)
            if n:
                self._touch(args[1])
            return b":%d\r\n" % n
        if cmd == "HKEYS":
            self._sweep()
            h = self.hashes.get(args[1]) or {}
            keys = list(h.keys())
            out = [b"*%d\r\n" % len(keys)]
            for k in keys:
                kb = k.encode()
                out.append(b"$%d\r\n%s\r\n" % (len(kb), kb))
            return b"".join(out)
        if cmd == "HINCRBY":
            self._sweep()
            h = self.hashes.setdefault(args[1], {})
            cur = int(h.get(args[2], "0")) + int(args[3])
            h[args[2]] = str(cur)
            self._touch(args[1])
            return b":%d\r\n" % cur
        # ---- set ----
        if cmd == "SADD":
            self._sweep()
            s = self.sets.setdefault(args[1], set())
            added = 0
            for m in args[2:]:
                if m not in s:
                    s.add(m)
                    added += 1
            if added:
                self._touch(args[1])
            return b":%d\r\n" % added
        if cmd == "SREM":
            self._sweep()
            s = self.sets.get(args[1]) or set()
            removed = 0
            for m in args[2:]:
                if m in s:
                    s.discard(m)
                    removed += 1
            if removed:
                self._touch(args[1])
            return b":%d\r\n" % removed
        if cmd == "SMEMBERS":
            self._sweep()
            s = self.sets.get(args[1]) or set()
            members = sorted(s)
            out = [b"*%d\r\n" % len(members)]
            for m in members:
                mb = m.encode()
                out.append(b"$%d\r\n%s\r\n" % (len(mb), mb))
            return b"".join(out)
        return b"-ERR unknown command\r\n"
