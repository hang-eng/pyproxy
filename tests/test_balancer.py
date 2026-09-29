"""负载均衡策略测试。

重点是两个容易写错的地方：

1. 轮询在有后端被剔除、或重试要跳过已试过的后端时，不能乱序或偏袒。
2. 加权策略只能从「健康」的后端里挑，且分布要符合权重。
"""

from __future__ import annotations

import itertools
import random
import threading
import unittest
from collections import Counter

from proxy.balancer import (
    Balancer,
    RandomBalancer,
    RoundRobinBalancer,
    WeightedRandomBalancer,
    WeightedRoundRobinBalancer,
    create_balancer,
)
from proxy.config import Backend


def make_backends(spec: str) -> list:
    """按 "a:1,b:2:3" 的形式造后端列表（用字母当 host，便于断言）。

    格式是 host:port 或 host:port:weight——注意别把权重当成端口写，
    要按权重造请用 weighted_backends()。
    """
    backends = []
    for item in spec.split(","):
        parts = item.split(":")
        if len(parts) == 2:
            backends.append(Backend(parts[0], int(parts[1]), 1))
        else:
            backends.append(Backend(parts[0], int(parts[1]), int(parts[2])))
    return backends


def weighted_backends(**weights) -> list:
    """按权重造后端：weighted_backends(a=5, b=1)。

    单独一个函数，是因为 "a:5" 那种写法里 5 是端口不是权重，
    很容易看错——加权相关的用例就全走这里，不再有歧义。
    """
    return [Backend(name, port, weight)
            for port, (name, weight) in enumerate(weights.items(), start=1)]


def pick_addresses(balancer: Balancer, times: int, **kwargs) -> list:
    return [balancer.select(**kwargs).address for _ in range(times)]


class TestRoundRobin(unittest.TestCase):

    def test_cycles_in_order(self):
        balancer = RoundRobinBalancer(make_backends("a:1,b:2,c:3"))
        self.assertEqual(pick_addresses(balancer, 6),
                         ["a:1", "b:2", "c:3", "a:1", "b:2", "c:3"])

    def test_single_backend(self):
        balancer = RoundRobinBalancer(make_backends("a:1"))
        self.assertEqual(pick_addresses(balancer, 3), ["a:1"] * 3)

    def test_skips_unhealthy(self):
        backends = make_backends("a:1,b:2,c:3")
        backends[1].healthy = False
        balancer = RoundRobinBalancer(backends)
        self.assertEqual(pick_addresses(balancer, 4), ["a:1", "c:3", "a:1", "c:3"])

    def test_all_unhealthy_returns_none(self):
        backends = make_backends("a:1,b:2")
        for b in backends:
            b.healthy = False
        self.assertIsNone(RoundRobinBalancer(backends).select())

    def test_exclude_is_honoured(self):
        balancer = RoundRobinBalancer(make_backends("a:1,b:2,c:3"))
        self.assertEqual(balancer.select(exclude={"a:1"}).address, "b:2")

    def test_exclude_everything_returns_none(self):
        balancer = RoundRobinBalancer(make_backends("a:1,b:2"))
        self.assertIsNone(balancer.select(exclude={"a:1", "b:2"}))

    def test_recovery_does_not_desync(self):
        """后端恢复后，轮转序列应当接着原来的节奏走。"""
        backends = make_backends("a:1,b:2,c:3")
        balancer = RoundRobinBalancer(backends)
        self.assertEqual(pick_addresses(balancer, 3), ["a:1", "b:2", "c:3"])
        backends[1].healthy = False
        self.assertEqual(pick_addresses(balancer, 2), ["a:1", "c:3"])
        backends[1].healthy = True
        # 不该出现某台被连着选中两次的情况
        self.assertEqual(pick_addresses(balancer, 3), ["a:1", "b:2", "c:3"])


class TestRoundRobinWithRetries(unittest.TestCase):
    """回归测试：重试跳过后端时轮询序列不能错位。

    这是实际踩到过的 bug。场景是 4 个后端 [A, 坏, B, C]，坏的那台每次
    都失败并触发一次重试。历史上有两种错法：

    * 把 ``self._index = (self._index + 1) % len(healthy)`` 写回状态：
      序号被坏后端反复拉回 0，**B 永远收不到请求**，实测得到 ACACAC……
    * 序号单调递增但对 len(healthy) 取模：正常轮转走 %4、重试走 %3，
      两个模循环对不齐，12 次请求里 A 拿 5 次、B 只拿 1 次、C 拿 6 次。

    现在的做法是序号对**固定总数**取模、遇到不在候选里的就跳过，
    所以下面这组断言必须成立。
    """

    def setUp(self):
        self.backends = make_backends("A:1,bad:2,B:3,C:4")
        self.balancer = RoundRobinBalancer(self.backends)

    def test_no_backend_is_starved(self):
        served = []
        for _ in range(12):
            chosen = self.balancer.select()
            served.append(chosen.address)
            if chosen.address == "bad:2":
                # 模拟「这一台失败了，换个后端重试」
                served.append(self.balancer.select(exclude={"bad:2"}).address)

        counts = Counter(a for a in served if a != "bad:2")
        self.assertEqual(counts, {"A:1": 4, "B:3": 4, "C:4": 4},
                         f"分布不均：{dict(counts)}，完整序列 {served}")

    def test_bad_backend_still_gets_selected(self):
        seen = [self.balancer.select().address for _ in range(12)]
        self.assertEqual(seen.count("bad:2"), 3, f"坏后端也被跳过了：{seen}")

    def test_excluded_one_never_repeats_immediately(self):
        chosen = self.balancer.select()
        nxt = self.balancer.select(exclude={chosen.address})
        self.assertNotEqual(nxt.address, chosen.address)


class TestRandom(unittest.TestCase):

    def test_only_healthy_picked(self):
        backends = make_backends("a:1,b:2,c:3")
        backends[0].healthy = False
        balancer = RandomBalancer(backends, rng=random.Random(1234))
        picked = set(pick_addresses(balancer, 50))
        self.assertEqual(picked, {"b:2", "c:3"})

    def test_none_when_all_unhealthy(self):
        backends = make_backends("a:1")
        backends[0].healthy = False
        self.assertIsNone(RandomBalancer(backends).select())

    def test_covers_all_backends(self):
        balancer = RandomBalancer(make_backends("a:1,b:2,c:3"), rng=random.Random(7))
        self.assertEqual(set(pick_addresses(balancer, 60)), {"a:1", "b:2", "c:3"})


class TestWeightedRoundRobin(unittest.TestCase):

    def test_distribution_follows_weight(self):
        balancer = WeightedRoundRobinBalancer(weighted_backends(a=1, b=1, c=1))
        counts = Counter(pick_addresses(balancer, 30))
        self.assertEqual(counts, {"a:1": 10, "b:2": 10, "c:3": 10})

    def test_skewed_weights(self):
        balancer = WeightedRoundRobinBalancer(weighted_backends(a=5, b=1))
        counts = Counter(pick_addresses(balancer, 60))
        self.assertEqual(counts, {"a:1": 50, "b:2": 10})

    def test_is_smooth_not_bursty(self):
        """权重 5:1:1 时不能连着发 5 次 A——那是朴素切片算法的行为。"""
        balancer = WeightedRoundRobinBalancer(weighted_backends(a=5, b=1, c=1))
        picked = pick_addresses(balancer, 7)
        longest_run = max(len(list(group)) for _, group in itertools.groupby(picked))
        self.assertLessEqual(longest_run, 2, f"出现连续 {longest_run} 次同一后端：{picked}")
        self.assertEqual(Counter(picked), {"a:1": 5, "b:2": 1, "c:3": 1})

    def test_only_healthy_get_traffic(self):
        backends = weighted_backends(a=1, b=1)
        backends[0].healthy = False
        balancer = WeightedRoundRobinBalancer(backends)
        self.assertEqual(set(pick_addresses(balancer, 10)), {"b:2"})

    def test_weight_zeroed_backend_gets_nothing(self):
        # effective_weight 为 0 表示"暂时别给它流量"（健康检查恢复时会重置）
        backends = weighted_backends(a=1, b=1)
        backends[0].effective_weight = 0
        balancer = WeightedRoundRobinBalancer(backends)
        self.assertEqual(set(pick_addresses(balancer, 10)), {"b:2"})

    def test_none_when_all_unhealthy(self):
        backends = weighted_backends(a=1)
        backends[0].healthy = False
        self.assertIsNone(WeightedRoundRobinBalancer(backends).select())


class TestWeightedRandom(unittest.TestCase):

    def test_distribution_follows_weight(self):
        balancer = WeightedRandomBalancer(weighted_backends(a=9, b=1),
                                          rng=random.Random(99))
        counts = Counter(pick_addresses(balancer, 1000))
        self.assertGreater(counts["a:1"], 850)
        self.assertLess(counts["a:1"], 950)

    def test_only_healthy(self):
        backends = weighted_backends(a=1, b=1)
        backends[1].healthy = False
        balancer = WeightedRandomBalancer(backends, rng=random.Random(3))
        self.assertEqual(set(pick_addresses(balancer, 20)), {"a:1"})


class TestCommonBehaviour(unittest.TestCase):

    def test_backends_property_is_a_copy(self):
        backends = make_backends("a:1")
        balancer = RoundRobinBalancer(backends)
        balancer.backends.append(Backend("z", 9))
        self.assertEqual(len(balancer.backends), 1, "外部不应能改到内部列表")

    def test_total_requests_counts_only_selections(self):
        backends = make_backends("a:1,b:2")
        balancer = RoundRobinBalancer(backends)
        for _ in range(4):
            balancer.select()
        # 一次都没选中的后端，计数必须还是 0，否则统计会虚高
        balancer.select(exclude={"a:1", "b:2"})
        self.assertEqual([b.total_requests for b in backends], [2, 2])

    def test_stats_shape(self):
        balancer = RoundRobinBalancer(make_backends("a:1,b:2:3"))
        stats = balancer.stats()
        self.assertEqual(stats["strategy"], "round_robin")
        self.assertEqual(len(stats["backends"]), 2)
        self.assertEqual(stats["backends"][1]["weight"], 3)
        self.assertIn("healthy", stats["backends"][0])

    def test_concurrent_select_is_thread_safe(self):
        """多线程并发挑选时计数不能丢。"""
        backends = make_backends("a:1,b:2,c:3,d:4")
        balancer = RoundRobinBalancer(backends)
        rounds, threads_count = 200, 8

        def worker():
            for _ in range(rounds):
                self.assertIsNotNone(balancer.select())

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = sum(b.total_requests for b in backends)
        self.assertEqual(total, rounds * threads_count)

    def test_base_class_is_abstract(self):
        with self.assertRaises(NotImplementedError):
            Balancer(make_backends("a:1"))._pick([])


class TestFactory(unittest.TestCase):

    def test_every_strategy_name_resolves(self):
        expected = {
            "round_robin": RoundRobinBalancer,
            "random": RandomBalancer,
            "weighted_round_robin": WeightedRoundRobinBalancer,
            "weighted_random": WeightedRandomBalancer,
        }
        for name, cls in expected.items():
            self.assertIsInstance(create_balancer(name, make_backends("a:1")), cls)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            create_balancer("nope", make_backends("a:1"))

    def test_rng_is_injectable(self):
        # 同一颗种子必须给出同一串结果，否则测试没法复现
        a = pick_addresses(RandomBalancer(make_backends("a:1,b:2,c:3"), rng=random.Random(5)), 10)
        b = pick_addresses(RandomBalancer(make_backends("a:1,b:2,c:3"), rng=random.Random(5)), 10)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
