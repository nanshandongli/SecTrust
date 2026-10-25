"""
跨机协同原语（transport/distributed.py）—— Redis 三件套：锁 / 心跳 / 死信。

用户路线图第 4 步「接 Redis 跨机：先支持 2–3 台机器，再逐步压测」的本模块。
同机多进程早已被 SQLite CAS 认领 + WAL 覆盖；跨机需要三样同机没有的东西：

  1. **分布式锁**（DistributedLock）：SET NX EX 原子抢锁，WATCH/MULTI/EXEC
     乐观事务做"比对 token 后才续期/释放"——防住"锁过期后别人的锁被自己
     续期/删掉"的经典竞态。用途：跨机任务认领守卫（两台机器同时看到同一
     任务，只有拿到锁的机器能干活）。
  2. **runner 心跳**（HeartbeatRegistry）：SET EX 心跳键 + 注册集合，
     alive/dead 判定；掉线机器清单交给上层决定重派或死信。
  3. **死信队列**（DeadLetterQueue）：失败计数达阈值才入死信（防一次抖动
     就进 DLQ），支持人工恢复（recover）与丢弃（drop）。

ClusterCoordinator 把三件套装在一起，from_env() 读开关：
  SECTRUST_REDIS=1 + REDIS_URL=redis://... → 启用（不可达则构造时即报错）
  未配置 → enabled=False，所有操作返回"未启用"语义（False/None/[]）

⚠️ 开关纪律（与 transport/queue.py build_queue 一致，不可破坏）：
  跨机场景**静默降级回单机语义是最危险的 bug**——调用方以为有分布式保护，
  其实没有。因此：强开（SECTRUST_REDIS=1）但 Redis 不可达 → 直接抛错；
  未开 → 明确返回未启用语义，绝不"假装成功"。

拓扑说明（诚实边界）：
  - 共享 SQLite（网络盘）：CAS 认领仍有效（WAL 走网络盘有风险，见文档），
    Redis 锁在上层再加一道跨机原子保护；
  - 每机本地库 + Redis 队列：RedisQueue 的 RPOP 本身就是跨机认领，
    本模块的心跳/死信依然有用。
  锁只保护"认领"这一刻；任务表本身怎么同步，取决于上面两种拓扑的选择。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import List, Optional


# 续期/释放锁的乐观事务：WATCH 锁键 → 读 token → 不匹配就放弃；
# 匹配则 MULTI 里排队写操作，EXEC 时若锁键被任何写改动过则整体作废。
# 这条路等价于 Lua 的 compare-and-set，但测试假服务器无需内置 Lua。

class DistributedLock:
    """SET NX EX 分布式锁 + WATCH 乐观事务续期/释放。

    token 是每次 acquire 生成的唯一串：只有持 token 者能 renew/release，
    且续期/释放走乐观事务——期间锁若被别人抢走（过期后被抢），操作作废，
    绝不会误续/误删别人的锁。
    """

    def __init__(self, client, key_prefix: str = "ai-colleague:lock:",
                 default_ttl: int = 30):
        self._client = client          # 调用方负责线程隔离（RESPClient 非线程安全）
        self.key_prefix = key_prefix
        self.default_ttl = default_ttl

    def _key(self, name: str) -> str:
        return self.key_prefix + name

    def acquire(self, name: str, owner: str, ttl: int = None) -> Optional[str]:
        """抢锁。成功返回 token（后续 renew/release 都要带），失败返回 None。"""
        ttl = int(ttl or self.default_ttl)
        token = f"{owner}:{os.getpid()}:{threading.get_ident()}:{time.time()}"
        ok = self._client.set_nx(self._key(name), token, ttl)
        return token if ok == "OK" else None

    def holder(self, name: str) -> Optional[str]:
        """当前持锁者的 token（含 owner 前缀）；无锁返回 None。"""
        return self._client.get(self._key(name))

    def renew(self, name: str, token: str, ttl: int = None) -> bool:
        """持 token 续期。锁已易主/消失 → False（调用方应停止干活）。"""
        ttl = int(ttl or self.default_ttl)
        key = self._key(name)
        c = self._client
        c.watch(key)
        try:
            if c.get(key) != token:
                return False
            c.multi()
            c.expire(key, ttl)
            return bool(c.exec())     # EXEC 返回 None = 被 WATCH 的键被动过，作废
        finally:
            try:
                c.unwatch()
            except Exception:  # noqa: BLE001 —— EXEC 后已自动清 WATCH，再 UNWATCH 无害
                pass

    def release(self, name: str, token: str) -> bool:
        """持 token 释放。锁已易主 → False（不能删别人的锁）。"""
        key = self._key(name)
        c = self._client
        c.watch(key)
        try:
            if c.get(key) != token:
                return False
            c.multi()
            c.delete(key)
            return bool(c.exec())
        finally:
            try:
                c.unwatch()
            except Exception:  # noqa: BLE001
                pass


class HeartbeatRegistry:
    """runner 心跳注册：SET EX 心跳键 + SADD 注册集合。

    掉线判定完全由 TTL 决定——不需要任何"最后一次心跳时间"的比较逻辑，
    Redis 自己过期。alive_runners() 顺手把集合里已过期的成员清掉。
    """

    def __init__(self, client, key_prefix: str = "ai-colleague:hb:",
                 default_ttl: int = 30):
        self._client = client
        self.key_prefix = key_prefix
        self.default_ttl = default_ttl

    def _hb_key(self, runner_id: str) -> str:
        return self.key_prefix + runner_id

    def _registry_key(self) -> str:
        return self.key_prefix + "registry"

    def beat(self, runner_id: str, meta: dict = None, ttl: int = None) -> bool:
        """报一次心跳。meta（如 bot_id/机器名）随心跳一起存，供人查。"""
        ttl = int(ttl or self.default_ttl)
        payload = json.dumps({"runner_id": runner_id, "at": time.time(),
                              "meta": meta or {}}, ensure_ascii=False)
        c = self._client
        # SET EX 覆盖写：心跳键只归本人，无竞态；注册集合记名供枚举
        c.execute("SET", self._hb_key(runner_id), payload, "EX", ttl)
        c.sadd(self._registry_key(), runner_id)
        return True

    def is_alive(self, runner_id: str) -> bool:
        return bool(self._client.exists(self._hb_key(runner_id)))

    def alive_runners(self) -> List[str]:
        """存活 runner 清单（顺手清理集合里已过期的成员）。"""
        c = self._client
        members = c.smembers(self._registry_key())
        alive = [m for m in members if c.exists(self._hb_key(m))]
        dead = [m for m in members if m not in alive]
        if dead:
            try:
                c.srem(self._registry_key(), *dead)
            except Exception:  # noqa: BLE001 —— 清理失败不影响本次查询
                pass
        return sorted(alive)

    def runner_info(self, runner_id: str) -> Optional[dict]:
        raw = self._client.get(self._hb_key(runner_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"runner_id": runner_id, "raw": raw}

    def drop(self, runner_id: str) -> None:
        """主动下线（优雅退出时调）：删心跳 + 移出注册集合。"""
        c = self._client
        try:
            c.delete(self._hb_key(runner_id))
            c.srem(self._registry_key(), runner_id)
        except Exception:  # noqa: BLE001
            pass


class DeadLetterQueue:
    """死信队列：失败计数达阈值才入队，支持恢复与丢弃。

    键位（key_prefix 默认 "ai-colleague:dlq:"）：
      {p}list      死信条目（JSON：task_id/reason/payload/at/attempts）
      {p}meta      每条死信的元数据 hash（按 task_id 去重/快速查）
      {p}fail      失败计数 hash（未达阈值前的抖动计数）
    """

    def __init__(self, client, key_prefix: str = "ai-colleague:dlq:",
                 threshold: int = 3):
        self._client = client
        self.key_prefix = key_prefix
        self.threshold = max(1, int(threshold))

    def _list_key(self) -> str:
        return self.key_prefix + "list"

    def _meta_key(self) -> str:
        return self.key_prefix + "meta"

    def _fail_key(self) -> str:
        return self.key_prefix + "fail"

    def note_failure(self, task_id: str, reason: str, payload: dict = None) -> bool:
        """记一次失败。达到阈值才真正入死信，返回是否已入队。

        低于阈值只累加计数（防单次抖动进 DLQ）；调用方通常接着重试。
        重复入队按 task_id 在读取侧去重（HINCRBY 原子，但"计数到达"与
        "入队"两步之间理论上有并发窗口，去重兜底）。
        """
        c = self._client
        n = c.hincrby(self._fail_key(), task_id, 1)
        if n < self.threshold:
            return False
        entry = {"task_id": task_id, "reason": str(reason), "attempts": n,
                 "at": time.time(), "payload": payload or {}}
        raw = json.dumps(entry, ensure_ascii=False, default=str)
        c.execute("LPUSH", self._list_key(), raw)
        c.hset(self._meta_key(), task_id, raw)
        c.hdel(self._fail_key(), task_id)
        return True

    def failures(self, task_id: str) -> int:
        """当前抖动计数（未达阈值时为剩余机会，已达阈值入队后为 0）。"""
        c = self._client
        try:
            raw = c.hget(self._fail_key(), task_id)
        except Exception:  # noqa: BLE001
            return 0
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def contains(self, task_id: str) -> bool:
        """task_id 是否已在死信中（O(1) 查 meta hash，不扫列表）。"""
        return self._client.hget(self._meta_key(), task_id) is not None

    def list(self, limit: int = 50) -> List[dict]:
        """死信清单（新→旧），按 task_id 去重（保留最新一条）。"""
        c = self._client
        raws = c.lrange(self._list_key(), 0, max(0, limit - 1))
        seen, out = set(), []
        for raw in raws:
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            tid = entry.get("task_id")
            if tid in seen:
                continue
            seen.add(tid)
            out.append(entry)
        return out

    def count(self) -> int:
        return len(self._client.lrange(self._list_key(), 0, -1))

    def recover(self, task_id: str) -> Optional[dict]:
        """从死信移除并返回条目（供上层重新派发）；不在死信中返回 None。

        返回最新一条。同一 task_id 可能有多条原始记录（重复入队），
        全部移除——恢复一次就该彻底离开死信，不留半条。
        """
        c = self._client
        first = None
        for raw in c.lrange(self._list_key(), 0, -1):
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.get("task_id") != task_id:
                continue
            c.lrem(self._list_key(), raw)
            if first is None:
                first = entry
        if first is not None:
            c.hdel(self._meta_key(), task_id)
        return first

    def drop(self, task_id: str) -> bool:
        """丢弃一条死信（不再恢复）。"""
        return self.recover(task_id) is not None


class ClusterCoordinator:
    """跨机协同总装：锁 + 心跳 + 死信，一个开关（from_env）管全部。

    用法：
        cluster = ClusterCoordinator.from_env()      # 未配置 → enabled=False
        if cluster.enabled:
            token = cluster.acquire("claim:" + task_id, runner_id)
            ...
            cluster.beat(runner_id)                  # 心跳循环里调
            cluster.note_failure(task_id, err)       # 失败路径调
    """

    def __init__(self, url: str = None, key_prefix: str = "ai-colleague:cluster:",
                 enabled: bool = None, lock_ttl: int = 30, heartbeat_ttl: int = 30,
                 dead_letter_after: int = 3):
        self.url = url or os.getenv("REDIS_URL", "")
        self.key_prefix = key_prefix
        self.lock_ttl = lock_ttl
        self.heartbeat_ttl = heartbeat_ttl
        self.dead_letter_after = dead_letter_after
        if enabled is None:
            enabled = (os.getenv("SECTRUST_REDIS", "").strip()
                       in ("1", "true", "yes") and bool(self.url))
        self.enabled = enabled
        self._local = threading.local()
        self._all_clients = []          # close() 关所有线程的连接
        self._lock_reentrant = threading.Lock()
        if self.enabled:
            self._probe()               # 强开但不可达 → 构造即抛，不静默降级

    # ---------- 开关 ----------
    @classmethod
    def from_env(cls, **kw) -> "ClusterCoordinator":
        return cls(url=os.getenv("REDIS_URL") or None, **kw)

    def _probe(self) -> None:
        from transport.resp import RESPClient
        c = RESPClient(self.url)
        try:
            if not c.ping():
                raise RuntimeError("Redis 探活失败（PING 无 PONG）")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"SECTRUST_REDIS 已开启但 Redis 不可达（{self.url}）：{e}。"
                f"跨机场景拒绝静默降级——请修好 Redis，或显式关闭开关。"
            ) from e
        finally:
            c.close()

    # ---------- 连接：线程隔离 + 可全关（与 CostStore 同纪律）----------
    @property
    def client(self):
        c = getattr(self._local, "client", None)
        if c is None:
            from transport.resp import RESPClient
            c = RESPClient(self.url).connect()
            self._local.client = c
            with self._lock_reentrant:
                self._all_clients.append(c)
        return c

    def close(self):
        with self._lock_reentrant:
            clients, self._all_clients = self._all_clients, []
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._local = threading.local()

    # ---------- 三件套（未启用时一律返回"未启用"语义）----------
    @property
    def lock(self) -> Optional[DistributedLock]:
        if not self.enabled:
            return None
        return DistributedLock(self.client, self.key_prefix + "lock:",
                               default_ttl=self.lock_ttl)

    @property
    def heartbeat(self) -> Optional[HeartbeatRegistry]:
        if not self.enabled:
            return None
        return HeartbeatRegistry(self.client, self.key_prefix + "hb:",
                                 default_ttl=self.heartbeat_ttl)

    @property
    def dead_letters(self) -> Optional[DeadLetterQueue]:
        if not self.enabled:
            return None
        return DeadLetterQueue(self.client, self.key_prefix + "dlq:",
                               threshold=self.dead_letter_after)

    # ---- 锁便捷方法 ----
    def acquire(self, name: str, owner: str, ttl: int = None) -> Optional[str]:
        return self.lock.acquire(name, owner, ttl) if self.enabled else None

    def renew(self, name: str, token: str, ttl: int = None) -> bool:
        return self.lock.renew(name, token, ttl) if self.enabled else False

    def release(self, name: str, token: str) -> bool:
        return self.lock.release(name, token) if self.enabled else False

    def holder(self, name: str) -> Optional[str]:
        return self.lock.holder(name) if self.enabled else None

    # ---- 任务认领守卫 ----
    def claim_task(self, task_id: str, runner_id: str, ttl: int = None
                   ) -> Optional[str]:
        """抢任务锁。拿到返回 token；拿不到（别的机器在干）返回 None；
        未启用返回 None——调用方据此区分"没开 Redis"与"锁被抢"，
        必须查 .enabled，不能把 None 一律当抢到。"""
        return self.acquire("claim:" + task_id, runner_id, ttl)

    def release_task(self, task_id: str, token: str) -> bool:
        return self.release("claim:" + task_id, token)

    # ---- 心跳便捷方法 ----
    def beat(self, runner_id: str, meta: dict = None) -> bool:
        return self.heartbeat.beat(runner_id, meta) if self.enabled else False

    def alive_runners(self) -> List[str]:
        return self.heartbeat.alive_runners() if self.enabled else []

    def is_alive(self, runner_id: str) -> bool:
        return self.heartbeat.is_alive(runner_id) if self.enabled else False

    # ---- 死信便捷方法 ----
    def note_failure(self, task_id: str, reason: str, payload: dict = None) -> bool:
        return (self.dead_letters.note_failure(task_id, reason, payload)
                if self.enabled else False)

    def dead_letter_list(self, limit: int = 50) -> List[dict]:
        return self.dead_letters.list(limit) if self.enabled else []

    def recover_dead_letter(self, task_id: str) -> Optional[dict]:
        return self.dead_letters.recover(task_id) if self.enabled else None
