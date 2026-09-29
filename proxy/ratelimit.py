"""按客户端 IP 限流（令牌桶）。

桶里最多攒 ``capacity`` 个令牌，以 ``refill_rate`` 个/秒的速度补充，
每个请求拿走一个；拿不到就拒绝（HTTP 429）。

三个实现上的选择，都是为了在真实压力下不出问题：

1. **惰性补充，不起定时器。** 每个客户端一个后台线程去补令牌，客户端一多
   就是几千个线程。改成"下次访问时按流逝时间一次算清"，代价是 O(1) 的
   乘法，收益是完全没有后台开销。空桶放着不管也不会占 CPU。
2. **用单调时钟（``time.monotonic``）而不是墙上时钟。** 系统对时或时区
   调整会让墙上时钟跳变，一旦往回跳，``elapsed`` 变成负数，桶就会
   凭空多出（或少掉）令牌。单调时钟只往前走。
3. **空闲桶要回收，而且上限必须是硬上限。** 键是客户端 IP，正常情况下会
   随时间无限增长——被扫段时几万个 IP 就能把内存吃光。回收分两步：先丢
   「已经补满」的桶（它和新建的桶行为完全一致，丢了不损失精度），不够
   再按最久未更新的顺序丢。只做第一步是不够的：扫段来的桶全是活跃桶，
   一个都回收不掉。回收只在桶数超上限时批量触发，不会让每个请求都背上
   O(n) 的扫描。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from .config import RateLimitConfig


class TokenBucket:
    """单个客户端的令牌桶。"""

    __slots__ = ("capacity", "refill_rate", "tokens", "updated_at")

    def __init__(self, capacity: int, refill_rate: float, now: float):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.tokens = float(capacity)   # 新客户端从满桶开始，不打第一枪
        self.updated_at = now

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.updated_at = now

    def is_full(self, now: float) -> bool:
        """补满的桶等价于新桶，可以安全回收。"""
        return self.tokens + (now - self.updated_at) * self.refill_rate >= self.capacity

    def take(self, now: float) -> bool:
        """尝试取走一个令牌。"""
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after(self, now: float) -> float:
        """还差多久才攒够一个令牌（秒）。"""
        self._refill(now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.refill_rate

    def snapshot(self, now: float) -> Dict:
        self._refill(now)
        return {
            "tokens": round(self.tokens, 3),
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
        }


class RateLimiter:
    """按客户端 IP 限流。线程安全，可被多个请求线程并发调用。"""

    def __init__(self, config: RateLimitConfig, max_buckets: int = 10000):
        self._config = config
        self._max_buckets = max_buckets
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._allowed = 0
        self._rejected = 0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def check(self, client_ip: str, now: Optional[float] = None) -> Tuple[bool, float]:
        """返回 (是否放行, 被拒时建议的 Retry-After 秒数)。"""
        if not self._config.enabled:
            return True, 0.0

        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    self._make_room(now)
                bucket = TokenBucket(
                    self._config.capacity, self._config.refill_rate, now
                )
                self._buckets[client_ip] = bucket

            if bucket.take(now):
                self._allowed += 1
                return True, 0.0

            self._rejected += 1
            return False, bucket.retry_after(now)

    def _make_room(self, now: float) -> None:
        """把桶数压回上限以内（调用方必须已持锁）。

        分两步：

        1. 先丢「已经补满」的桶。它和一个刚建出来的新桶行为完全一致，
           丢掉不损失任何限流精度。正常流量下这一步就够了。
        2. 如果丢完仍然在上限之上，说明桶全是活跃的（比如正在被扫段），
           那就按最久未更新的顺序再丢一批。

        第 2 步不是可选项。只做第 1 步的话，几万个不同 IP 打过来，
        每个桶都刚被取走令牌，一个也回收不掉——字典照样无限长，
        内存照样爆。而"上限"存在的全部意义就是别让自己先被打挂。

        代价是被第 2 步丢掉的活跃客户端会拿到一个满桶，相当于白送一次
        突发额度。这是内存安全与限流精度之间的取舍，在被打挂和略微
        放宽限流之间，选后者。

        一次批量丢到 90% 而不是精确丢一个：排序是 O(n log n)，如果
        每来一个新客户端就排一次，等于给自己造了个 CPU 放大攻击。
        摊薄成每 10% 的量级排一次，平均成本可以忽略。
        """
        stale = [ip for ip, bucket in self._buckets.items() if bucket.is_full(now)]
        for ip in stale:
            del self._buckets[ip]

        if len(self._buckets) < self._max_buckets:
            return

        # 至少要腾出一个位置，否则调用方插入后立刻又超限
        target = max(0, min(len(self._buckets) - 1, int(self._max_buckets * 0.9)))
        oldest = sorted(self._buckets.items(), key=lambda item: item[1].updated_at)
        for ip, _bucket in oldest[: len(self._buckets) - target]:
            del self._buckets[ip]

    def stats(self, now: Optional[float] = None) -> Dict:
        """导出限流状态，供 Web 管理界面使用。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            return {
                "enabled": self._config.enabled,
                "capacity": self._config.capacity,
                "refill_rate": self._config.refill_rate,
                "tracked_clients": len(self._buckets),
                "max_buckets": self._max_buckets,
                "allowed": self._allowed,
                "rejected": self._rejected,
            }

    def reset(self) -> None:
        """清空所有桶与计数（测试与热更新用）。"""
        with self._lock:
            self._buckets.clear()
            self._allowed = 0
            self._rejected = 0
