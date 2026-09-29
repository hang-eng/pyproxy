"""Web 管理界面（``/__admin``）的测试。

两件事最要紧：

1. **它只对本机开放。** 页面暴露的是后端拓扑（内网地址、权重、健康状态）
   和限流参数。远程客户端必须拿到 404 —— 不是 403，403 等于告诉对方
   「这儿确实有东西」。这条要用真实的非回环地址验证，不能只测判定函数。
2. **页面上的数字要真的对得上。** 一个显示假数据的监控页比没有监控页更糟，
   所以统计口径（什么算一次请求、重试怎么算）都得有用例钉住。
"""

from __future__ import annotations

import socket
import unittest

from proxy.admin import ADMIN_PATH, is_admin_path, is_local_client
from proxy.config import RateLimitConfig

from .support import ProxyFixture, header, request, wait_for_stats
from .test_proxy import BackendTestCase


def _lan_ip():
    """找一个本机的非回环 IPv4 地址；找不到就返回 None。

    用 UDP socket 连一下外部地址（不会真的发包）拿到默认出口网卡的地址。
    拿不到说明这台机器没有可用网络，相关用例跳过。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 53))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


class TestAdminPathMatching(unittest.TestCase):
    """哪些路径算管理页。"""

    def test_exact_path_matches(self):
        self.assertTrue(is_admin_path("/__admin"))

    def test_trailing_slash_matches(self):
        self.assertTrue(is_admin_path("/__admin/"))

    def test_query_string_is_ignored(self):
        self.assertTrue(is_admin_path("/__admin?x=1&y=2"))

    def test_prefix_is_not_enough(self):
        # /__administrator 不是管理页；用前缀匹配会把它误判进来
        self.assertFalse(is_admin_path("/__administrator"))

    def test_nested_path_is_not_admin(self):
        self.assertFalse(is_admin_path("/api/__admin"))

    def test_similar_paths_are_not_admin(self):
        for path in ("/", "/admin", "/_admin", "/__adminx", "/index.html"):
            self.assertFalse(is_admin_path(path), path)

    def test_empty_path(self):
        self.assertFalse(is_admin_path(""))
        self.assertFalse(is_admin_path(None))


class TestLocalClientDetection(unittest.TestCase):
    """回环地址的判定。"""

    def test_ipv4_loopback(self):
        self.assertTrue(is_local_client(("127.0.0.1", 5000)))

    def test_any_127_x_x_x_is_loopback(self):
        self.assertTrue(is_local_client(("127.9.9.9", 5000)))

    def test_ipv6_loopback(self):
        self.assertTrue(is_local_client(("::1", 5000)))

    def test_ipv4_mapped_ipv6_loopback(self):
        # 双栈监听时 127.0.0.1 会以 ::ffff:127.0.0.1 的形式出现，
        # 认不出来就会把本机运维挡在门外
        self.assertTrue(is_local_client(("::ffff:127.0.0.1", 5000)))

    def test_remote_addresses_are_rejected(self):
        for host in ("8.8.8.8", "192.168.1.10", "10.0.0.1", "::ffff:8.8.8.8"):
            self.assertFalse(is_local_client((host, 5000)), host)

    def test_hostname_instead_of_ip_is_rejected(self):
        """不能因为看着像就放行——解析不了的一律当远程处理。"""
        self.assertFalse(is_local_client(("localhost", 5000)))

    def test_missing_address_is_rejected(self):
        self.assertFalse(is_local_client(None))
        self.assertFalse(is_local_client(()))


class TestAdminPage(BackendTestCase):
    """页面本身。"""

    def test_local_client_gets_the_page(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port, ADMIN_PATH)
        self.assertEqual(status, 200)
        self.assertEqual(header(headers, "Content-Type"),
                         "text/html; charset=utf-8")

    def test_page_is_not_cacheable(self):
        """页面每 2 秒自刷新，中间任何一层缓存住了就看不到实时状态。"""
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _, headers, _ = request(fx.port, ADMIN_PATH)
        self.assertEqual(header(headers, "Cache-Control"), "no-store")

    def test_page_self_refreshes(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _, _, body = request(fx.port, ADMIN_PATH)
        self.assertIn(b'http-equiv="refresh" content="2"', body)

    def test_page_lists_backends_with_weights_and_health(self):
        backend_a, _ = self.start_backend("A", weight=3)
        backend_b, _ = self.start_backend("B", weight=1)
        with ProxyFixture([backend_a, backend_b]) as fx:
            _, _, body = request(fx.port, ADMIN_PATH)
        text = body.decode("utf-8")
        self.assertIn(backend_a.address, text)
        self.assertIn(backend_b.address, text)
        self.assertIn("健康", text)
        self.assertIn("已转发", text)

    def test_page_does_not_leak_python_version(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _, _, body = request(fx.port, ADMIN_PATH)
        self.assertNotIn(b"Python", body)

    def test_page_escapes_backend_addresses(self):
        """地址里塞标签不该被当 HTML 渲染——注入点在配置文件里也得防住。"""
        from proxy.admin import render
        from proxy.config import ProxyConfig
        from proxy.server import ProxyContext

        config = ProxyConfig(listen_host="127.0.0.1", listen_port=8080)
        config.health_check.enabled = False
        ctx = ProxyContext(config, _EvilBalancer())
        markup = render(ctx, 1.0).decode("utf-8")
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_page_shows_rate_limit_state(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend],
                          rate_limit=RateLimitConfig(enabled=True, capacity=5,
                                                     refill_rate=2.0)) as fx:
            _, _, body = request(fx.port, ADMIN_PATH)
        text = body.decode("utf-8")
        self.assertIn("已启用", text)
        self.assertIn("放行", text)

    def test_page_says_disabled_when_rate_limit_is_off(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _, _, body = request(fx.port, ADMIN_PATH)
        self.assertIn("已关闭", body.decode("utf-8"))

    def test_head_request_has_headers_but_no_body(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port, ADMIN_PATH, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        # Content-Length 描述的仍然是 GET 会返回多少
        self.assertGreater(int(header(headers, "Content-Length")), 0)


class _EvilBalancer:
    """故意在地址里塞 HTML 的假均衡器，用来验证转义。"""

    def stats(self):
        return {
            "strategy": "round_robin",
            "backends": [
                {
                    "address": "<script>alert(1)</script>",
                    "weight": 1,
                    "healthy": True,
                    "total_requests": 0,
                }
            ],
        }


class TestAdminStats(BackendTestCase):
    """页面上的数字要对得上。"""

    def test_request_count_and_bytes(self):
        backend, _ = self.start_backend("A", body=b"0123456789")
        with ProxyFixture([backend]) as fx:
            for _ in range(3):
                request(fx.port)
            stats = wait_for_stats(fx.server.ctx, total_requests=3, total_bytes=30)
        self.assertEqual(stats["total_requests"], 3)
        self.assertEqual(stats["total_bytes"], 30)

    def test_status_classes_are_split(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port)
            request(fx.port, "/nope", method="CONNECT")
            stats = wait_for_stats(fx.server.ctx, total_requests=2, status_4xx=1)
        self.assertEqual(stats["total_requests"], 2)
        # CONNECT 被代理拒了（405），也要计数——扫描器很爱发它，
        # 这类请求如果不留痕，「谁在扫我」就永远看不出来
        self.assertEqual(stats["status_4xx"], 1)

    def test_5xx_from_backend_is_counted(self):
        backend, _ = self.start_backend("A", status=503)
        with ProxyFixture([backend]) as fx:
            self.assertEqual(request(fx.port)[0], 503)
            stats = wait_for_stats(fx.server.ctx, status_5xx=1)
        self.assertEqual(stats["status_5xx"], 1)

    def test_proxy_generated_5xx_is_counted(self):
        """连不上后端时代理自己发的 502 也要计入，否则故障是隐形的。"""
        from proxy.config import Backend
        from tests.support import free_port

        dead = Backend("127.0.0.1", free_port(), 1)
        with ProxyFixture([dead], max_retries=0) as fx:
            self.assertEqual(request(fx.port)[0], 502)
            stats = wait_for_stats(fx.server.ctx, status_5xx=1)
        self.assertEqual(stats["status_5xx"], 1)

    def test_retries_are_counted(self):
        """重试次数要能看出来：它是后端不稳的最早信号。"""
        backend_a, _ = self.start_backend("A", fail_times=999)  # A 永远不回话
        backend_b, _ = self.start_backend("B")
        with ProxyFixture([backend_a, backend_b], max_retries=2) as fx:
            for _ in range(20):
                self.assertEqual(request(fx.port)[0], 200)
            stats = wait_for_stats(fx.server.ctx, total_requests=20)
        self.assertGreaterEqual(stats["total_retries"], 1)

    def test_uptime_is_positive(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            self.assertGreater(fx.server.ctx.uptime, 0.0)

    def test_stats_survive_without_an_access_logger(self):
        """访问日志可以关掉，管理页的数字不该跟着一起消失。"""
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            self.assertIsNone(fx.server.ctx.access_logger)
            request(fx.port)
            stats = wait_for_stats(fx.server.ctx, total_requests=1)
        self.assertEqual(stats["total_requests"], 1)

    def test_concurrent_requests_are_counted_exactly(self):
        """统计是跨请求线程累加的，少一个锁就会丢数。"""
        import threading

        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            def hammer():
                for _ in range(20):
                    request(fx.port)

            threads = [threading.Thread(target=hammer) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            stats = wait_for_stats(fx.server.ctx, total_requests=100)
        self.assertEqual(stats["total_requests"], 100)


class TestAdminIsNotProxied(BackendTestCase):
    """/__admin 不能被转发到后端。"""

    def test_backend_never_sees_admin_requests(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, ADMIN_PATH)
        self.assertEqual(records, [])

    def test_backend_that_looks_like_the_admin_path_is_still_proxied(self):
        """后端的 /__administrator 是正常业务路径，不该被管理页吃掉。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, _, _ = request(fx.port, "/__administrator")
        self.assertEqual(status, 200)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "/__administrator")


class TestAdminIsLoopbackOnly(BackendTestCase):
    """远程客户端看不到管理页——这条必须用真实地址验证。

    只测 `is_local_client()` 是不够的：真正要证明的是「从非回环地址
    连过来的请求，代理会怎么处理」。所以这里把代理绑到 0.0.0.0，
    再从本机的局域网地址连过去，让 `client_address` 真的是远程 IP。
    """

    def setUp(self):
        self.lan_ip = _lan_ip()
        if not self.lan_ip:
            self.skipTest("本机没有可用的非回环地址，无法模拟远程客户端")

    def fixture(self, backends):
        return ProxyFixture(backends, listen_host="0.0.0.0")

    def test_remote_client_gets_404_not_the_page(self):
        backend, _ = self.start_backend("A")
        with self.fixture([backend]) as fx:
            status, _, body = request(fx.port, ADMIN_PATH, host=self.lan_ip)
        self.assertEqual(status, 404)
        self.assertNotIn("运行状态".encode("utf-8"), body)

    def test_remote_client_gets_404_not_403(self):
        """403 等于承认「这儿确实有东西」，会给探测的人一条线索。

        所以对外只回一个标准的 Not Found：不带任何权限类首部，
        响应体里也不回显这个路径名。
        """
        backend, _ = self.start_backend("A")
        with self.fixture([backend]) as fx:
            status, headers, body = request(fx.port, ADMIN_PATH, host=self.lan_ip)
        self.assertEqual(status, 404)
        self.assertFalse(
            any(key.lower() in ("www-authenticate", "allow") for key, _ in headers)
        )
        self.assertIn(b"Not Found", body)
        self.assertNotIn(b"__admin", body)

    def test_remote_client_can_still_use_the_proxy(self):
        """管理页对外关闭，但代理本身当然要对外服务。"""
        backend, _ = self.start_backend("A")
        with self.fixture([backend]) as fx:
            status, headers, body = request(fx.port, "/app", host=self.lan_ip)
        self.assertEqual(status, 200)
        self.assertEqual(header(headers, "X-Backend-Name"), "A")
        self.assertEqual(body, b"ok")

    def test_remote_admin_requests_are_logged(self):
        """探测管理页这件事本身要留痕，否则被人扫了都不知道。

        「管理页不计入统计」只对**成功渲染**的那一次成立；远程探测拿到
        的 404 必须照常计数——那才是需要被看见的事。
        """
        backend, _ = self.start_backend("A")
        with self.fixture([backend]) as fx:
            request(fx.port, ADMIN_PATH, host=self.lan_ip)
            stats = wait_for_stats(fx.server.ctx, total_requests=1, status_4xx=1)
        self.assertEqual(stats["total_requests"], 1)
        self.assertEqual(stats["status_4xx"], 1)


if __name__ == "__main__":
    unittest.main()
