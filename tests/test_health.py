"""健康检查测试。

分两层：

* **状态机**（apply_result）——纯逻辑，不需要网络，直接喂探测结果。
* **探测**（_probe / check_once）——对真实的模拟后端发请求。

分开测的价值在于：状态机的边界条件（连续计数、阈值、恢复时清零权重）
用真实网络很难精确构造，而探测逻辑又容易被状态机的细节掩盖。
"""

from __future__ import annotations

import time
import unittest

from proxy.config import Backend, HealthCheckConfig
from proxy.health import HealthChecker

from tests.support import make_backend, start_server, wait_for


def make_checker(backends, on_change=None, **config_kwargs) -> HealthChecker:
    config = HealthCheckConfig(
        enabled=config_kwargs.pop("enabled", True),
        interval=config_kwargs.pop("interval", 0.05),
        timeout=config_kwargs.pop("timeout", 0.5),
        path=config_kwargs.pop("path", "/health"),
        unhealthy_threshold=config_kwargs.pop("unhealthy_threshold", 3),
        healthy_threshold=config_kwargs.pop("healthy_threshold", 2),
        **config_kwargs,
    )
    return HealthChecker(backends, config, on_change=on_change)


class TestStateMachine(unittest.TestCase):
    """用 apply_result 直接驱动状态迁移。"""

    def setUp(self):
        self.backend = Backend("127.0.0.1", 1)
        self.checker = make_checker([self.backend])

    def fail(self, times: int) -> None:
        for _ in range(times):
            self.checker.apply_result(self.backend, False)

    def succeed(self, times: int) -> None:
        for _ in range(times):
            self.checker.apply_result(self.backend, True)

    def test_starts_healthy(self):
        # 乐观启动：代理一起来就能用，不必等第一轮探测跑完
        self.assertTrue(self.backend.healthy)

    def test_needs_consecutive_failures_to_evict(self):
        self.fail(2)
        self.assertTrue(self.backend.healthy, "没到阈值就剔除了")
        self.fail(1)
        self.assertFalse(self.backend.healthy, "到阈值了却没剔除")

    def test_single_failure_does_not_evict(self):
        # 这一条是防抖动的核心：网络抖一下不该把后端踢出去
        self.fail(1)
        self.assertTrue(self.backend.healthy)

    def test_success_resets_failure_streak(self):
        self.fail(2)
        self.succeed(1)
        self.assertEqual(self.backend.consecutive_failures, 0)
        self.fail(2)
        self.assertTrue(self.backend.healthy, "失败计数没被成功清零")

    def test_needs_consecutive_successes_to_recover(self):
        self.fail(3)
        self.assertFalse(self.backend.healthy)
        self.succeed(1)
        self.assertFalse(self.backend.healthy, "只成功一次就恢复了")
        self.succeed(1)
        self.assertTrue(self.backend.healthy, "连续成功够了却没恢复")

    def test_failure_resets_success_streak(self):
        self.fail(3)
        self.succeed(1)
        self.fail(1)
        self.succeed(1)
        self.assertFalse(self.backend.healthy, "成功计数没被失败清零")

    def test_recovery_resets_current_weight(self):
        """恢复时必须丢掉挂掉前累积的加权值。

        否则平滑加权轮询会把这个后端连着选中好几次——它"欠"了太多。
        """
        self.backend.current_weight = 42
        self.fail(3)
        self.succeed(2)
        self.assertTrue(self.backend.healthy)
        self.assertEqual(self.backend.current_weight, 0)

    def test_eviction_keeps_current_weight(self):
        # 剔除时不清权重，因为恢复那一步才需要清；这里只确认剔除本身没副作用
        self.backend.current_weight = 7
        self.fail(3)
        self.assertEqual(self.backend.current_weight, 7)


class TestCallbacks(unittest.TestCase):

    def setUp(self):
        self.backend = Backend("127.0.0.1", 1)

    def test_on_change_fires_once_per_transition(self):
        changes = []
        checker = make_checker([self.backend], on_change=changes.append)

        checker.apply_result(self.backend, False)
        checker.apply_result(self.backend, False)
        self.assertEqual(changes, [], "状态没变不该回调")

        checker.apply_result(self.backend, False)      # -> 不健康
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0].healthy)

        checker.apply_result(self.backend, True)
        self.assertEqual(len(changes), 1, "还没恢复就回调了")

        checker.apply_result(self.backend, True)       # -> 恢复
        self.assertEqual(len(changes), 2)
        self.assertTrue(changes[1].healthy)

    def test_callback_exception_does_not_break_state_machine(self):
        def boom(_backend):
            raise RuntimeError("回调炸了")

        checker = make_checker([self.backend], on_change=boom)
        for _ in range(3):
            checker.apply_result(self.backend, False)   # 不该抛出去
        self.assertFalse(self.backend.healthy)


class TestThreadLifecycle(unittest.TestCase):

    def test_disabled_does_not_start_thread(self):
        checker = make_checker([Backend("127.0.0.1", 1)], enabled=False)
        checker.start()
        self.assertFalse(checker.running)
        checker.stop()

    def test_start_stop(self):
        checker = make_checker([Backend("127.0.0.1", 1)], interval=0.02)
        checker.start()
        try:
            self.assertTrue(checker.running)
            self.assertTrue(wait_for(lambda: checker.rounds >= 2, timeout=5.0),
                            "探测线程没有推进轮数")
        finally:
            checker.stop()
        self.assertFalse(checker.running)

    def test_stop_is_idempotent(self):
        checker = make_checker([Backend("127.0.0.1", 1)], interval=0.02)
        checker.start()
        checker.stop()
        checker.stop()          # 再停一次不该抛
        self.assertFalse(checker.running)

    def test_start_twice_is_harmless(self):
        checker = make_checker([Backend("127.0.0.1", 1)], interval=0.02)
        checker.start()
        try:
            checker.start()     # 已在跑就不该再起一个线程
            time.sleep(0.1)
            self.assertTrue(checker.running)
        finally:
            checker.stop()

    def test_rounds_advance_via_check_once(self):
        checker = make_checker([Backend("127.0.0.1", 1)])
        self.assertEqual(checker.rounds, 0)
        checker.check_once()
        checker.check_once()
        self.assertEqual(checker.rounds, 2)


class TestProbe(unittest.TestCase):
    """对真实的模拟后端发探测请求。"""

    def setUp(self):
        self.servers = []
        self.addCleanup(self._stop_servers)

    def _stop_servers(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def start(self, name, **options) -> int:
        cls, _records = make_backend(name, **options)
        server = start_server(cls)
        self.servers.append(server)
        return server.server_address[1]

    def test_healthy_backend_probed_ok(self):
        port = self.start("ok")
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend])
        checker.check_once()
        self.assertTrue(backend.healthy)
        self.assertEqual(backend.consecutive_successes, 1)

    def test_probe_hits_configured_path(self):
        # 探测路径配错了会把活后端误判成死的，所以这条必须测
        cls, records = make_backend("probe")
        server = start_server(cls)
        self.servers.append(server)
        backend = Backend("127.0.0.1", server.server_address[1])

        checker = make_checker([backend], path="/status")
        checker.check_once()
        self.assertEqual(records[0]["path"], "/status")
        self.assertTrue(backend.healthy)

    def test_dead_backend_is_probed_unhealthy(self):
        from tests.support import free_port
        backend = Backend("127.0.0.1", free_port())
        checker = make_checker([backend])
        for _ in range(3):
            checker.check_once()
        self.assertFalse(backend.healthy)

    def test_5xx_counts_as_unhealthy(self):
        port = self.start("err", status=500)
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend])
        for _ in range(3):
            checker.check_once()
        self.assertFalse(backend.healthy)

    def test_4xx_counts_as_unhealthy(self):
        # 404 说明探测路径配错了，本身就该当异常处理
        port = self.start("nf", status=404)
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend])
        for _ in range(3):
            checker.check_once()
        self.assertFalse(backend.healthy)

    def test_3xx_counts_as_healthy(self):
        # 重定向说明服务活着，只是不想直接答这个路径
        port = self.start("redir", status=302)
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend])
        checker.check_once()
        self.assertTrue(backend.healthy)

    def test_probe_timeout_counts_as_failure(self):
        port = self.start("slow", delay=1.0)
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend], timeout=0.2)
        for _ in range(3):
            checker.check_once()
        self.assertFalse(backend.healthy)

    def test_backend_going_down_is_detected(self):
        """后端在运行中挂掉，探测要能发现。"""
        cls, _records = make_backend("doomed")
        server = start_server(cls)
        port = server.server_address[1]
        backend = Backend("127.0.0.1", port)
        checker = make_checker([backend], timeout=0.3)

        checker.check_once()
        self.assertTrue(backend.healthy)

        server.shutdown()
        server.server_close()

        for _ in range(3):
            checker.check_once()
        self.assertFalse(backend.healthy, "后端已经关了却还判为健康")

    def test_check_once_propagates_probe_exception(self):
        """探测函数自身抛异常时，check_once 会往外抛——兜底的是 _loop。"""
        backend = Backend("127.0.0.1", 1)
        checker = make_checker([backend])

        def boom(_backend):
            raise RuntimeError("探测炸了")

        checker._probe = boom
        with self.assertRaises(RuntimeError):
            checker.check_once()

    def test_loop_survives_probe_exception(self):
        """上面那个异常必须被 _loop 挡住，线程不能死。"""
        def boom(_backend):
            raise RuntimeError("探测炸了")

        checker = make_checker([Backend("127.0.0.1", 1)], interval=0.02)
        checker._probe = boom
        checker.start()
        try:
            time.sleep(0.3)
            self.assertTrue(checker.running, "异常把探测线程打死了")
        finally:
            checker.stop()

    def test_stop_interrupts_sleep(self):
        """stop() 必须能立刻叫醒等待中的线程，不能干等一个周期。"""
        checker = make_checker([Backend("127.0.0.1", 1)], interval=30.0)
        checker.start()
        time.sleep(0.1)
        started = time.time()
        checker.stop()
        self.assertLess(time.time() - started, 5.0, "stop() 在傻等间隔")


class TestSnapshot(unittest.TestCase):

    def test_snapshot_reports_state(self):
        backends = [Backend("a", 1), Backend("b", 2)]
        backends[1].healthy = False
        backends[1].consecutive_failures = 3
        backends[0].total_requests = 5
        checker = make_checker(backends)

        snapshot = checker.snapshot()
        self.assertEqual([row["address"] for row in snapshot], ["a:1", "b:2"])
        self.assertEqual(snapshot[0]["total_requests"], 5)
        self.assertFalse(snapshot[1]["healthy"])
        self.assertEqual(snapshot[1]["consecutive_failures"], 3)
        self.assertTrue(snapshot[0]["healthy"])


if __name__ == "__main__":
    unittest.main()
