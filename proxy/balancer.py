"""负载均衡策略。

提供四种策略：

    round_robin            轮询
    random                 随机
    weighted_round_robin   平滑加权轮询（nginx 同款算法）
    weighted_random        加权随机

三条约定：

1. 只从「健康」的后端中挑选。一个健康后端都没有时返回 ``None``，
   由调用方转成 HTTP 503，而不是抛异常。
2. 每个策略实例自带一把锁，可被多个请求线程并发调用。
3. 读取 ``backend.healthy`` 是单次属性读取，在 CPython 下是原子操作，
   因此不必与健康检查线程共用锁（详见 README 的并发模型一节）。
"""

from __future__ import annotations

import random
import threading
from typing import Dict, Iterable, List, Optional, Set, Type

from .config import Backend


class Balancer:
    """负载均衡策略基类。

    子类只需实现 ``_pick``：入参保证是「非空的健康后端列表」。
    """

    name = "base"

    def __init__(self, backends: Iterable[Backend], rng: Optional[random.Random] = None):
        self._backends: List[Backend] = list(backends)
        self._lock = threading.Lock()
        self._rng = rng if rng is not None else random.Random()

    @property
    def backends(self) -> List[Backend]:
        """后端列表的浅拷贝，避免外部直接改动内部结构。"""
        return list(self._backends)

    def select(self, exclude: Optional[Set[str]] = None) -> Optional[Backend]:
        """选出一个后端；没有可用后端时返回 None。

        exclude 是「这次请求已经试过、失败了的后端地址」集合。重试时必须
        换一台后端——如果只在同一台上重试，那台机器真挂了的话重试多少次
        都没用，纯粹是把超时时间叠加起来。

        过滤放在 select 里而不是让调用方反复调用 select 直到拿到没试过的，
        是为了让 total_requests 只在真正选定一台时加一，不虚增计数。
        """
        with self._lock:
            healthy = [b for b in self._backends if b.healthy]
            if exclude:
                healthy = [b for b in healthy if b.address not in exclude]
            if not healthy:
                return None
            backend = self._pick(healthy)
            backend.total_requests += 1
            return backend

    def _pick(self, healthy: List[Backend]) -> Backend:
        raise NotImplementedError

    def stats(self) -> Dict:
        """导出运行时统计，供 Web 管理界面使用。"""
        with self._lock:
            return {
                "strategy": self.name,
                "backends": [
                    {
                        "address": b.address,
                        "weight": b.weight,
                        "healthy": b.healthy,
                        "total_requests": b.total_requests,
                    }
                    for b in self._backends
                ],
            }


class RoundRobinBalancer(Balancer):
    """轮询：按顺序依次分发，到末尾回到开头。

    关键是**取模的基数必须是固定的后端总数**（``len(self._backends)``），
    而不是当前候选列表的长度。候选列表会变——健康检查会剔除后端，重试会
    用 exclude 过滤掉已经试过的后端——对「会变的长度」取模会让轮转序列
    错位。

    实测到的两种错法（4 个后端 A/坏/B/C，坏的那个每次都失败并触发重试）：

    * 把 ``self._index = (self._index + 1) % len(healthy)`` 写回状态：
      序号被坏后端反复拉回 0，B **永远收不到请求**。
    * 序号单调递增但对 len(healthy) 取模：正常轮转走 ``% 4``、重试走 ``% 3``，
      两个模循环对不齐，12 次请求里 A 拿 5 次、B 只拿 1 次、C 拿 6 次。

    现在的做法：序号对固定总数取模，选到不在候选里的就跳过并继续前进。
    这样轮转基准恒定，坏后端只是被「跳过」而不打乱节奏，12 次请求下
    A/B/C 各拿 4 次。
    """

    name = "round_robin"

    def __init__(self, backends: Iterable[Backend], rng: Optional[random.Random] = None):
        super().__init__(backends, rng)
        self._index = 0

    def _pick(self, healthy: List[Backend]) -> Backend:
        allowed = {b.address for b in healthy}
        count = len(self._backends)
        for _ in range(count):
            backend = self._backends[self._index % count]
            self._index = (self._index + 1) % count
            if backend.address in allowed:
                return backend
        # healthy 非空且必是 self._backends 的子集，走不到这里
        return healthy[0]


class RandomBalancer(Balancer):
    """随机：等概率挑选。"""

    name = "random"

    def _pick(self, healthy: List[Backend]) -> Backend:
        return self._rng.choice(healthy)


class WeightedRoundRobinBalancer(Balancer):
    """平滑加权轮询（smooth weighted round-robin）。

    每轮先把每个健康后端的 effective_weight 累加到自己的 current_weight，
    选出 current_weight 最大的那个，再把它减去「健康后端权重之和」。

    这样做的好处是请求分布均匀交错。对比朴素的「按权重切片依次发」
    （权重 5:1:1 会连续发 5 次 A 再发 B），平滑算法会得到
    A B A C A A A 这样的序列，瞬时压力更平均。
    """

    name = "weighted_round_robin"

    def _pick(self, healthy: List[Backend]) -> Backend:
        total_weight = 0
        best: Optional[Backend] = None

        for backend in healthy:
            backend.current_weight += backend.effective_weight
            total_weight += backend.effective_weight
            if best is None or backend.current_weight > best.current_weight:
                best = backend

        assert best is not None  # healthy 非空，循环至少执行一次
        best.current_weight -= total_weight
        return best


class WeightedRandomBalancer(Balancer):
    """加权随机：按权重作为概率挑选。"""

    name = "weighted_random"

    def _pick(self, healthy: List[Backend]) -> Backend:
        weights = [b.effective_weight for b in healthy]
        return self._rng.choices(healthy, weights=weights, k=1)[0]


STRATEGY_CLASSES: Dict[str, Type[Balancer]] = {
    "round_robin": RoundRobinBalancer,
    "random": RandomBalancer,
    "weighted_round_robin": WeightedRoundRobinBalancer,
    "weighted_random": WeightedRandomBalancer,
}


def create_balancer(
    strategy: str,
    backends: Iterable[Backend],
    rng: Optional[random.Random] = None,
) -> Balancer:
    """按策略名创建负载均衡器。

    rng 用于注入可复现的随机源，单元测试里会传入固定种子的 Random。
    """
    try:
        cls = STRATEGY_CLASSES[strategy]
    except KeyError:
        raise ValueError(f"未知的负载均衡策略：{strategy!r}") from None
    return cls(backends, rng=rng)
