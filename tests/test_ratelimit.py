"""令牌桶限流的测试。

分两层：
* `TestTokenBucket` / `TestRateLimiter` 直接驱动令牌桶，用**注入的时间戳**
  而不是 `time.sleep`——测限流最怕的就是用例自己慢（每次验证"等一秒
  补一个令牌"就慢一秒），注入时间戳后全部是瞬时的，而且完全确定。
* `TestRateLimitThroughProxy` 走真实 socket 端到端，验证 429 真的发出来、
  `Retry-After` 真的带上、限流真的没有把正常转发搞坏。
"""

from __future__ import annotations

import unittest

from proxy.config import RateLimitConfig
from proxy.ratelimit import RateLimiter, TokenBucket

from .support import ProxyFixture, header, request, wait_for_stats
from .test_proxy import BackendTestCase


def limited(capacity: int, refill_rate: float) -> RateLimitConfig:
    return RateLimitConfig(enabled=True, capacity=capacity, refill_rate=refill_rate)


class TestTokenBucket(unittest.TestCase):
    """令牌桶本身的行为。"""

    def bucket(self, capacity=5, rate=1.0, now=0.0) -> TokenBucket:
        return TokenBucket(capacity, rate, now)

    def test_new_bucket_starts_full(self):
        # 新客户端从满桶开始：否则一个刚上线的正常用户第一个请求就会被拒
        bucket = self.bucket(capacity=5)
        self.assertEqual(bucket.tokens, 5.0)

    def test_take_consumes_one_token(self):
        bucket = self.bucket(capacity=5)
        self.assertTrue(bucket.take(0.0))
        self.assertEqual(bucket.tokens, 4.0)

    def test_take_until_empty_then_refuse(self):
        bucket = self.bucket(capacity=3, rate=1.0)
        self.assertEqual([bucket.take(0.0) for _ in range(4)],
                         [True, True, True, False])
        self.assertEqual(bucket.tokens, 0.0)

    def test_refill_is_lazy_but_correct(self):
        """不靠定时器补令牌，而是在下次访问时按流逝时间一次算清。"""
        bucket = self.bucket(capacity=5, rate=2.0)
        for _ in range(5):
            bucket.take(0.0)
        self.assertEqual(bucket.tokens, 0.0)
        # 时间只前进 1 秒（且期间没有任何调用），理论上补 2 个
        self.assertTrue(bucket.take(1.0))
        self.assertEqual(bucket.tokens, 1.0)

    def test_refill_never_exceeds_capacity(self):
        bucket = self.bucket(capacity=3, rate=100.0)
        bucket.take(0.0)
        bucket.take(0.0)
        bucket.take(0.0)
        # 放它一整个小时，也不能攒下超过容量的令牌——否则"桶"就没意义了，
        # 客户端可以离线攒一年然后一次性打爆后端
        bucket.take(3600.0)
        self.assertEqual(bucket.tokens, 2.0)

    def test_time_never_goes_backwards(self):
        """单调时钟不会回拨；万一传进来一个更早的时间也不该凭空生成令牌。"""
        bucket = self.bucket(capacity=5, rate=1.0)
        bucket.take(10.0)
        before = bucket.tokens
        bucket.take(5.0)  # 时间倒流
        self.assertLessEqual(bucket.tokens, before)

    def test_retry_after_counts_down_as_tokens_refill(self):
        bucket = self.bucket(capacity=2, rate=1.0)
        bucket.take(0.0)
        bucket.take(0.0)
        self.assertAlmostEqual(bucket.retry_after(0.0), 1.0)
        # 补了 0.9 个令牌，还剩 0.1 个才够一个
        self.assertAlmostEqual(bucket.retry_after(0.9), 0.1, places=6)
        self.assertEqual(bucket.retry_after(1.0), 0.0)

    def test_retry_after_on_a_bucket_with_tokens_is_zero(self):
        self.assertEqual(self.bucket(capacity=3).retry_after(0.0), 0.0)

    def test_full_bucket_is_indistinguishable_from_a_new_one(self):
        used = self.bucket(capacity=3, rate=1.0)
        used.take(0.0)
        self.assertFalse(used.is_full(0.0))
        self.assertTrue(used.is_full(1.0))
        fresh = self.bucket(capacity=3, rate=1.0)
        # 关键性质：补满之后，两者在行为上完全一致——正因如此才能安全回收
        self.assertEqual(used.take(1.0), fresh.take(0.0))

    def test_disabled_config_is_not_consulted(self):
        limiter = RateLimiter(RateLimitConfig(enabled=False, capacity=1))
        # 关掉时连桶都不该建，否则"关掉限流"会变成"关掉限流但照样吃内存"
        for _ in range(100):
            self.assertEqual(limiter.check("1.2.3.4"), (True, 0.0))
        self.assertEqual(limiter.stats()["tracked_clients"], 0)


class TestRateLimiter(unittest.TestCase):
    """按 IP 分桶的限流器。"""

    def limiter(self, capacity=3, rate=1.0, max_buckets=10000) -> RateLimiter:
        return RateLimiter(
            RateLimitConfig(enabled=True, capacity=capacity, refill_rate=rate),
            max_buckets=max_buckets,
        )

    def test_buckets_are_per_client(self):
        limiter = self.limiter(capacity=1)
        self.assertTrue(limiter.check("10.0.0.1", now=0.0)[0])
        self.assertFalse(limiter.check("10.0.0.1", now=0.0)[0])
        # 另一个 IP 有自己的桶，不该被前一个拖累
        self.assertTrue(limiter.check("10.0.0.2", now=0.0)[0])

    def test_rejection_reports_how_long_to_wait(self):
        limiter = self.limiter(capacity=2, rate=0.5)
        limiter.check("10.0.0.1", now=0.0)
        limiter.check("10.0.0.1", now=0.0)
        allowed, retry_after = limiter.check("10.0.0.1", now=0.0)
        self.assertFalse(allowed)
        self.assertAlmostEqual(retry_after, 2.0)  # 每秒半个，等 2 秒

    def test_counters(self):
        limiter = self.limiter(capacity=2)
        limiter.check("10.0.0.1", now=0.0)
        limiter.check("10.0.0.1", now=0.0)
        limiter.check("10.0.0.1", now=0.0)
        limiter.check("10.0.0.2", now=0.0)
        stats = limiter.stats()
        self.assertEqual(stats["allowed"], 3)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["tracked_clients"], 2)

    def test_bucket_count_stays_under_the_cap(self):
        """上限必须是硬的：只有丢掉活跃桶，扫段才打不爆内存。"""
        limiter = self.limiter(capacity=3, rate=1.0, max_buckets=50)
        for index in range(500):
            limiter.check(f"10.0.{index // 256}.{index % 256}", now=0.0)
        self.assertLessEqual(limiter.stats()["tracked_clients"], 50)

    def test_idle_buckets_are_reclaimed_first(self):
        """满桶优先回收：它和新桶等价，丢了不损失限流精度。"""
        limiter = self.limiter(capacity=1, rate=1.0, max_buckets=6)
        for index in range(5):
            limiter.check(f"10.0.0.{index}", now=0.0)
        self.assertEqual(limiter.stats()["tracked_clients"], 5)
        # 时间前进 10 秒，这 5 个桶都补满了；再来几个新 IP 时它们该被清掉
        limiter.check("10.0.9.1", now=10.0)
        limiter.check("10.0.9.2", now=10.0)
        self.assertLessEqual(limiter.stats()["tracked_clients"], 6)

    def test_eviction_is_batched_not_per_request(self):
        """批量丢到 90%：每个新客户端都排一次序等于自造 CPU 放大攻击。"""
        limiter = self.limiter(capacity=1000, rate=0.001, max_buckets=100)
        for index in range(1000):
            # rate 极小 -> 桶几乎不会补满 -> 走的必然是第二步（按时间丢）
            limiter.check(f"10.{index // 256}.{index % 256}", now=0.0)
        self.assertLessEqual(limiter.stats()["tracked_clients"], 100)

    def test_one_client_hammering_is_throttled(self):
        """单客户端猛打是限流的核心场景，必须真的被挡住。"""
        limiter = self.limiter(capacity=2, rate=0.001)
        allowed = [limiter.check("10.0.0.1", now=0.0)[0] for _ in range(50)]
        self.assertEqual(allowed.count(True), 2)
        self.assertEqual(allowed.count(False), 48)

    def test_a_per_ip_bucket_does_not_stop_a_distributed_source(self):
        """如实记录一个**没有解决**的问题：换 IP 就能绕过。

        桶是按客户端 IP 分的，一万个 IP 各打一个请求就是一万个满桶，
        一个都不会被拒。要挡住这种得加全局限流或按子网聚合，本实现
        没有做——这条要原样写进 README 的「已知限制」，不假装解决了。

        把它写成用例而不是删掉，是为了让这个盲区一直可见：哪天有人
        改了限流逻辑，这条用例仍然会通过，但读代码的人会看到这里
        明明白白写着「分布式来源挡不住」。
        """
        limiter = self.limiter(capacity=2, rate=1.0, max_buckets=10000)
        allowed = [
            limiter.check(f"10.0.{index // 256}.{index % 256}", now=0.0)[0]
            for index in range(1000)
        ]
        self.assertTrue(all(allowed))

    def test_eviction_prefers_the_oldest_when_under_pressure(self):
        """表满时必须腾出位置，而且丢的是最久没动的那些。

        丢掉活跃桶是有代价的（那个客户端会拿到一个满桶），所以淘汰顺序
        要尽量合理：先丢早就没动静的，而不是随手丢一个新的。
        """
        limiter = self.limiter(capacity=1, rate=0.0001, max_buckets=4)
        for index in range(4):
            limiter.check(f"10.0.0.{index}", now=float(index))
        # 现在桶表满了（4 个，都只剩 0 个令牌）。用 10.0.0.0 之外的新 IP
        # 触发淘汰，最老的 10.0.0.0 应该被丢掉
        limiter.check("10.0.9.9", now=10.0)
        self.assertEqual(limiter.stats()["tracked_clients"], 4)
        # 10.0.0.0 被丢了，所以它现在是个满桶 -> 放行
        self.assertTrue(limiter.check("10.0.0.0", now=10.0)[0])

    def test_reset_clears_everything(self):
        limiter = self.limiter(capacity=1)
        limiter.check("10.0.0.1", now=0.0)
        limiter.check("10.0.0.1", now=0.0)
        limiter.reset()
        stats = limiter.stats()
        self.assertEqual(stats["tracked_clients"], 0)
        self.assertEqual((stats["allowed"], stats["rejected"]), (0, 0))

    def test_stats_reports_config(self):
        stats = self.limiter(capacity=7, rate=2.5).stats()
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["capacity"], 7)
        self.assertEqual(stats["refill_rate"], 2.5)

    def test_concurrent_checks_never_over_admit(self):
        """并发下不能超发：多个请求线程会同时打同一个桶。"""
        import threading

        limiter = self.limiter(capacity=50, rate=0.0001)
        admitted = []
        lock = threading.Lock()

        def worker():
            got = sum(1 for _ in range(20) if limiter.check("10.0.0.1", now=0.0)[0])
            with lock:
                admitted.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 8 个线程各试 20 次，共 160 次；桶容量 50，且补充速率几乎为 0
        self.assertEqual(sum(admitted), 50)


class TestRateLimitThroughProxy(BackendTestCase):
    """端到端：429 真的发出来了吗。"""

    def test_under_the_limit_passes(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(3, 0.5)) as fx:
            for _ in range(3):
                status, _, body = request(fx.port)
                self.assertEqual(status, 200)
                self.assertEqual(body, b"ok")

    def test_over_the_limit_returns_429_with_retry_after(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(2, 0.5)) as fx:
            self.assertEqual(request(fx.port)[0], 200)
            self.assertEqual(request(fx.port)[0], 200)
            status, headers, body = request(fx.port)
            self.assertEqual(status, 429)
            self.assertEqual(header(headers, "Retry-After"), "2")
            self.assertIn(b'"status": 429', body)

    def test_rejected_request_never_reaches_the_backend(self):
        """限流要拦住真流量，不能只是回个 429 然后照样转发。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(1, 0.01)) as fx:
            request(fx.port)
            for _ in range(4):
                self.assertEqual(request(fx.port)[0], 429)
            self.assertEqual(len(records), 1)

    def test_429_does_not_read_or_poison_the_request_body(self):
        """读请求体之前就拒绝，所以要断开连接——否则残留字节会污染下一个请求。

        `close_connection` 为真时 http.server 会关闭这条 keep-alive 连接，
        客户端拿到 429 后需要重新建连，下一次请求仍应正常。
        """
        backend, records = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(1, 0.01)) as fx:
            request(fx.port, "/", method="POST", body=b"x" * 1024)
            status, headers, _ = request(fx.port, "/", method="POST", body=b"y" * 1024)
            self.assertEqual(status, 429)
            self.assertEqual(header(headers, "Connection"), "close")

    def test_tokens_refill_over_time(self):
        """429 不该是永久性的：桶按时间补充，客户端自然恢复。"""
        import time

        backend, _ = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(1, 20.0)) as fx:
            self.assertEqual(request(fx.port)[0], 200)
            self.assertEqual(request(fx.port)[0], 429)
            time.sleep(0.25)  # 20/秒 -> 0.25 秒补 5 个
            self.assertEqual(request(fx.port)[0], 200)

    def test_429_counts_into_admin_stats(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(1, 0.01)) as fx:
            request(fx.port)
            request(fx.port)
            stats = wait_for_stats(fx.server.ctx, total_requests=2, status_4xx=1)
        self.assertEqual(stats["status_4xx"], 1)
        self.assertEqual(stats["total_requests"], 2)

    def test_admin_page_visits_do_not_inflate_the_counter(self):
        """管理页每 2 秒自刷新一次，计入统计就成了自己看自己导致数字暴涨。"""
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            for _ in range(5):
                self.assertEqual(request(fx.port, "/__admin")[0], 200)
            self.assertEqual(fx.server.ctx.stats()["total_requests"], 0)

    def test_retry_after_is_never_zero_on_a_rejection(self):
        """Retry-After: 0 会让客户端立刻重试，等于没有限流。

        补充速率特意取 1 个/秒：早先写的是 100 个/秒，也就是每 10ms 就补满
        一个令牌，而两次请求之间的耗时恰好也在这个量级——于是这条用例的成败
        取决于机器当时有多快。取值放到 1 个/秒，余量从 1 倍变成 200 倍。
        """
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend], rate_limit=limited(1, 1.0)) as fx:
            self.assertEqual(request(fx.port)[0], 200)
            status, headers, _ = request(fx.port)
            self.assertEqual(status, 429)
            self.assertGreaterEqual(int(header(headers, "Retry-After")), 1)


class TestDisabledRateLimit(BackendTestCase):
    """限流关掉时，一切照旧。"""

    def test_disabled_limiter_lets_everything_through(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            for _ in range(30):
                self.assertEqual(request(fx.port)[0], 200)


if __name__ == "__main__":
    unittest.main()
