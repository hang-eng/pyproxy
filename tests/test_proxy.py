"""转发核心测试：走真实的 socket，一个模拟后端 + 一个真代理。

不用 mock 替掉 http.client，是因为这一层最容易错的地方恰恰在协议细节上
（逐跳首部、分块编码的重新分帧、重复首部的保序、长连接语义），
把网络换掉这些就测不出来了。

夹具见 tests/support.py。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest

from proxy.access_log import AccessLogger
from proxy.config import Backend, ProxyConfig
from proxy.server import HOP_BY_HOP, MAX_BODY_BYTES, create_server

from tests.support import (
    ProxyFixture,
    free_port,
    header,
    header_values,
    make_backend,
    raw_exchange,
    request,
    start_server,
    wait_for,
)


class BackendTestCase(unittest.TestCase):
    """能起模拟后端与代理的公共基类。"""

    def start_backend(self, name: str, **options):
        cls, records = make_backend(name, **options)
        server = start_server(cls)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return Backend("127.0.0.1", server.server_address[1], options.get("weight", 1)), records


class TestBasicForwarding(BackendTestCase):

    def test_get_is_forwarded_and_response_relayed(self):
        backend, _ = self.start_backend("A", body=b"hello")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port, "/some/path?a=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello")
        self.assertEqual(header(headers, "X-Backend-Name"), "A")

    def test_path_and_query_preserved(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, "/a/b/c?x=1&y=2")
        self.assertEqual(records[0]["path"], "/a/b/c?x=1&y=2")

    def test_method_is_preserved(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
                request(fx.port, "/", method=method)
        self.assertEqual([r["method"] for r in records],
                         ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

    def test_request_body_forwarded(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, "/echo", method="POST", body=b"payload-123")
        self.assertEqual(records[0]["body"], b"payload-123")

    def test_backend_status_passed_through(self):
        backend, _ = self.start_backend("A", status=404, body=b"nope")
        with ProxyFixture([backend]) as fx:
            status, _headers, body = request(fx.port)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"nope")

    def test_backend_500_passed_through(self):
        backend, _ = self.start_backend("A", status=500)
        with ProxyFixture([backend]) as fx:
            status, _, _ = request(fx.port)
        self.assertEqual(status, 500)

    def test_absolute_form_request_line(self):
        """代理按 RFC 必须接受 GET http://host/path 这种写法。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            raw = (f"GET http://127.0.0.1:{fx.port}/abs/path?q=1 HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{fx.port}\r\nConnection: close\r\n\r\n").encode()
            response = raw_exchange(fx.port, raw)
        self.assertIn(b" 200 ", response.split(b"\r\n")[0])
        self.assertEqual(records[0]["path"], "/abs/path?q=1")

    def test_large_binary_body_survives(self):
        import hashlib
        payload = os.urandom(1024 * 1024)
        digest = hashlib.sha256(payload).hexdigest()
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, _, _ = request(fx.port, "/big", method="POST", body=payload)

        self.assertEqual(status, 200)
        # 1MiB 走完整条链路后必须一个字节都不差
        self.assertEqual(len(records[0]["body"]), len(payload))
        self.assertEqual(hashlib.sha256(records[0]["body"]).hexdigest(), digest)


class TestHeaderHandling(BackendTestCase):

    def test_hop_by_hop_headers_stripped(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, "/", headers={
                "Connection": "keep-alive",
                "Keep-Alive": "timeout=5",
                "Proxy-Authorization": "Basic xyz",
                "TE": "trailers",
                "Upgrade": "websocket",
                "X-Keep-Me": "yes",
            })
        sent = {k.lower(): v for k, v in records[0]["headers"]}
        for name in ("keep-alive", "proxy-authorization", "te", "upgrade"):
            self.assertNotIn(name, sent, f"逐跳首部 {name} 被转发给后端了")
        self.assertEqual(sent.get("x-keep-me"), "yes", "端到端首部不该被误删")

    def test_headers_named_by_connection_are_stripped(self):
        """Connection 里点名的字段也是逐跳的，即便它不在固定名单里。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, "/", headers={"Connection": "X-Custom-Hop"})
        sent = {k.lower() for k, _ in records[0]["headers"]}
        self.assertNotIn("x-custom-hop", sent)

    def test_upstream_connection_is_close(self):
        # 每个请求新建一条到后端的连接，用完即关，不该复用
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port)
            request(fx.port)
        for record in records:
            sent = {k.lower(): v for k, v in record["headers"]}
            self.assertEqual(sent.get("connection"), "close")

    def test_connection_header_not_leaked_to_client(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _status, headers, _ = request(fx.port)
        self.assertIsNone(header(headers, "Keep-Alive"))
        self.assertIsNone(header(headers, "Proxy-Authenticate"))

    def test_x_forwarded_for_injected(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port)
        sent = {k.lower(): v for k, v in records[0]["headers"]}
        self.assertEqual(sent.get("x-forwarded-for"), "127.0.0.1")

    def test_x_forwarded_for_appends_not_overwrites(self):
        """前面还有别的代理时，覆盖会丢掉真正的客户端 IP。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, headers={"X-Forwarded-For": "203.0.113.9"})
        sent = {k.lower(): v for k, v in records[0]["headers"]}
        self.assertEqual(sent.get("x-forwarded-for"), "203.0.113.9, 127.0.0.1")

    def test_x_forwarded_host_and_proto(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, headers={"Host": "example.com"})
        sent = {k.lower(): v for k, v in records[0]["headers"]}
        self.assertEqual(sent.get("x-forwarded-host"), "example.com")
        self.assertEqual(sent.get("x-forwarded-proto"), "http")
        # Host 本身按原样透传，后端可据此做虚拟主机路由
        self.assertEqual(sent.get("host"), "example.com")

    def test_duplicate_request_headers_preserved(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            raw = (f"GET / HTTP/1.1\r\nHost: h\r\nX-Dup: one\r\nX-Dup: two\r\n"
                   f"Connection: close\r\n\r\n").encode()
            raw_exchange(fx.port, raw)
        values = [v for k, v in records[0]["raw_pairs"] if k == "x-dup"]
        self.assertEqual(values, ["one", "two"], "重复的请求首部被合并或丢掉了")

    def test_duplicate_response_headers_preserved(self):
        backend, _ = self.start_backend("A", response_headers=(
            ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
        ))
        with ProxyFixture([backend]) as fx:
            _status, headers, _ = request(fx.port)
        self.assertEqual(header_values(headers, "Set-Cookie"), ["a=1", "b=2"])

    def test_via_header_added(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _status, headers, _ = request(fx.port)
        self.assertTrue(header(headers, "Via"))

    def test_backend_server_header_is_relayed_not_overwritten(self):
        # 反代不该改后端的 Server 首部，只该原样转发。
        # （末尾那个空格是 BaseHTTPRequestHandler 拼 version_string 时留下的，
        #  原样转发正是我们要的行为）
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            _status, headers, _ = request(fx.port)
        self.assertEqual(header(headers, "Server").strip(), "MockBackend/1.0")

    def test_proxy_error_response_does_not_leak_python_version(self):
        """代理自己生成的错误响应不能把 Python 版本号暴露出去。"""
        dead = Backend("127.0.0.1", free_port())
        with ProxyFixture([dead]) as fx:
            _status, headers, _ = request(fx.port)
        server_header = header(headers, "Server") or ""
        self.assertNotIn("Python", server_header)
        self.assertIn("MiniProxy", server_header)

    def test_hop_by_hop_from_backend_stripped(self):
        backend, _ = self.start_backend("A", response_headers=(("Upgrade", "h2c"),))
        with ProxyFixture([backend]) as fx:
            _status, headers, _ = request(fx.port)
        self.assertIsNone(header(headers, "Upgrade"))

    def test_hop_by_hop_constant_matches_rfc(self):
        # 名单本身也断言一下，免得日后被人"顺手"改掉
        self.assertEqual(HOP_BY_HOP, {
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade",
        })


class TestFraming(BackendTestCase):

    def test_chunked_request_is_decoded_and_reframed(self):
        """客户端用分块发，代理收全之后必须自己重算 Content-Length。

        这里手写裸报文而不用 http.client，是为了确保线上跑的**真的**是
        chunked——用库函数的话，万一它悄悄改用了 Content-Length，
        测试照样会"通过"，但测的东西已经不对了。
        """
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            raw = (b"POST /chunked HTTP/1.1\r\n"
                   b"Host: h\r\n"
                   b"Transfer-Encoding: chunked\r\n"
                   b"Connection: close\r\n\r\n"
                   b"6\r\npart1-\r\n"
                   b"6\r\npart2-\r\n"
                   b"5\r\npart3\r\n"
                   b"0\r\n\r\n")
            response = raw_exchange(fx.port, raw)

        self.assertIn(b" 200 ", response.split(b"\r\n")[0])
        self.assertEqual(records[0]["body"], b"part1-part2-part3")

        sent = {k.lower(): v for k, v in records[0]["headers"]}
        self.assertNotIn("transfer-encoding", sent, "分块已解开，不能再声明 chunked")
        self.assertEqual(sent.get("content-length"), "17")

    def test_chunked_response_is_decoded_and_reframed(self):
        """后端用分块回，代理解出来之后同样要重算长度。"""
        backend, _ = self.start_backend("A", chunked=True, body=b"chunky-data")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"chunky-data")
        self.assertIsNone(header(headers, "Transfer-Encoding"))
        self.assertEqual(header(headers, "Content-Length"), str(len(b"chunky-data")))

    def test_zero_length_post(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, _, _ = request(fx.port, "/empty", method="POST", body=b"")
        self.assertEqual(status, 200)
        self.assertEqual(records[0]["body"], b"")

    def test_head_has_no_body_but_keeps_length(self):
        backend, _ = self.start_backend("A", body=b"12345")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        # HEAD 的 Content-Length 描述的是"GET 会返回多少"，要透传
        self.assertEqual(header(headers, "Content-Length"), "5")

    def test_204_has_no_body(self):
        backend, _ = self.start_backend("A", status=204, body=b"")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(fx.port)
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_304_has_no_body(self):
        backend, _ = self.start_backend("A", status=304, body=b"")
        with ProxyFixture([backend]) as fx:
            status, _, body = request(fx.port)
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_keep_alive_across_requests(self):
        """同一个客户端连接上连发多个请求，代理要能撑住长连接。"""
        backend, _ = self.start_backend("A", body=b"x")
        with ProxyFixture([backend]) as fx:
            conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=15)
            try:
                for i in range(5):
                    conn.request("GET", f"/{i}")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"x")
            finally:
                conn.close()

    def test_client_disconnect_does_not_kill_server(self):
        """客户端中途断开，代理不该崩，后续请求照常。"""
        import socket
        backend, _ = self.start_backend("A", body=b"y")
        with ProxyFixture([backend]) as fx:
            sock = socket.create_connection(("127.0.0.1", fx.port), 10)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: h\r\n\r\n")
            sock.close()          # 不等响应就走
            status, _, body = request(fx.port)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"y")


class TestErrorHandling(BackendTestCase):

    def test_502_when_backend_unreachable(self):
        dead = Backend("127.0.0.1", free_port())
        with ProxyFixture([dead]) as fx:
            status, _headers, body = request(fx.port)
        self.assertEqual(status, 502)
        payload = json.loads(body)
        self.assertIn("无法连接后端", payload["detail"])
        self.assertEqual(payload["backend"], dead.address)

    def test_504_when_backend_times_out(self):
        backend, _ = self.start_backend("slow", delay=2.0)
        with ProxyFixture([backend], timeout=0.4) as fx:
            status, _headers, body = request(fx.port)
        self.assertEqual(status, 504)
        self.assertIn("没有响应", json.loads(body)["detail"])

    def test_503_when_no_backend_available(self):
        backend, _ = self.start_backend("A")
        backend.healthy = False
        with ProxyFixture([backend]) as fx:
            status, _headers, body = request(fx.port)
        self.assertEqual(status, 503)
        self.assertIn("所有后端都不可用", json.loads(body)["detail"])

    def test_405_for_connect(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=10)
            try:
                conn.request("CONNECT", "example.com:443")
                response = conn.getresponse()
                status = response.status
                body = response.read()
            finally:
                conn.close()
        self.assertEqual(status, 405)
        self.assertIn("CONNECT", json.loads(body)["detail"])

    def test_405_for_trace(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=10)
            try:
                conn.request("TRACE", "/")
                response = conn.getresponse()
                status = response.status
                response.read()
            finally:
                conn.close()
        self.assertEqual(status, 405)

    def test_error_response_is_json(self):
        dead = Backend("127.0.0.1", free_port())
        with ProxyFixture([dead]) as fx:
            _status, headers, body = request(fx.port)
        self.assertEqual(header(headers, "Content-Type"), "application/json; charset=utf-8")
        self.assertIn("detail", json.loads(body))

    def test_bad_content_length_returns_400(self):
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            raw = (b"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: abc\r\n"
                   b"Connection: close\r\n\r\n")
            response = raw_exchange(fx.port, raw)
        self.assertIn(b" 400 ", response.split(b"\r\n")[0])


class TestRetry(BackendTestCase):

    def test_idempotent_request_retries_on_another_backend(self):
        bad, bad_records = self.start_backend("bad", fail_times=99)
        good, good_records = self.start_backend("good", body=b"recovered")
        with ProxyFixture([bad, good], max_retries=2) as fx:
            status, headers, body = request(fx.port)

        self.assertEqual(status, 200)
        self.assertEqual(body, b"recovered")
        self.assertEqual(header(headers, "X-Backend-Name"), "good")
        self.assertEqual(len(bad_records), 1, "坏后端应该只被试一次")
        self.assertEqual(len(good_records), 1, "重试应该换到另一台")

    def test_post_is_not_retried(self):
        """POST 不是幂等的：重试可能造成重复下单，绝对不重试。"""
        bad, bad_records = self.start_backend("bad", fail_times=99)
        good, good_records = self.start_backend("good")
        with ProxyFixture([bad, good], max_retries=2) as fx:
            status, _, _ = request(fx.port, "/order", method="POST", body=b"buy")

        self.assertEqual(status, 502)
        self.assertEqual(len(bad_records), 1)
        self.assertEqual(len(good_records), 0, "非幂等方法不该被重试到别的后端")

    def test_put_and_delete_are_retried(self):
        for method in ("PUT", "DELETE", "HEAD", "OPTIONS"):
            bad, _ = self.start_backend("bad", fail_times=99)
            good, good_records = self.start_backend("good")
            with ProxyFixture([bad, good], max_retries=2) as fx:
                status, _, _ = request(fx.port, "/", method=method)
            self.assertEqual(status, 200, f"{method} 应该被重试")
            self.assertEqual(len(good_records), 1, f"{method} 没有换后端重试")

    def test_patch_is_not_retried(self):
        bad, _ = self.start_backend("bad", fail_times=99)
        good, good_records = self.start_backend("good")
        with ProxyFixture([bad, good], max_retries=2) as fx:
            status, _, _ = request(fx.port, "/", method="PATCH", body=b"x")
        self.assertEqual(status, 502)
        self.assertEqual(len(good_records), 0)

    def test_retry_respects_max_retries(self):
        backends = [self.start_backend(f"bad{i}", fail_times=99)[0] for i in range(4)]
        with ProxyFixture(backends, max_retries=1) as fx:
            status, _, _ = request(fx.port)
        self.assertEqual(status, 502)

    def test_no_retry_when_max_retries_is_zero(self):
        bad, bad_records = self.start_backend("bad", fail_times=99)
        good, good_records = self.start_backend("good")
        with ProxyFixture([bad, good], max_retries=0) as fx:
            status, _, _ = request(fx.port)
        self.assertEqual(status, 502)
        self.assertEqual(len(bad_records), 1)
        self.assertEqual(len(good_records), 0)

    def test_backend_generated_500_is_not_retried(self):
        """后端自己回的 5xx 是业务响应，不是转发失败，重试只会打扰它。"""
        backend, records = self.start_backend("boom", status=500)
        good, good_records = self.start_backend("good")
        with ProxyFixture([backend, good], max_retries=2) as fx:
            status, _, _ = request(fx.port)
        self.assertEqual(status, 500)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(good_records), 0)

    def test_503_when_every_backend_already_tried(self):
        backends, records = [], []
        for i in range(2):
            backend, rec = self.start_backend(f"bad{i}", fail_times=99)
            backends.append(backend)
            records.append(rec)
        with ProxyFixture(backends, max_retries=9) as fx:
            status, _, _ = request(fx.port)
        # 两台都试过、都失败之后，没有候选可用了
        self.assertEqual(status, 502)
        self.assertEqual(sum(len(r) for r in records), 2)

    def test_retry_skips_unhealthy_backend(self):
        bad, bad_records = self.start_backend("bad", fail_times=99)
        good, good_records = self.start_backend("good")
        bad.healthy = False
        with ProxyFixture([bad, good], max_retries=2) as fx:
            status, _, _ = request(fx.port)
        self.assertEqual(status, 200)
        self.assertEqual(len(bad_records), 0, "已剔除的后端不该再被选中")
        self.assertEqual(len(good_records), 1)


class TestBalancingThroughProxy(BackendTestCase):

    def test_round_robin_through_real_proxy(self):
        backends, names = [], []
        records = {}
        for name in ("A", "B", "C"):
            backend, rec = self.start_backend(name)
            backends.append(backend)
            names.append(name)
            records[name] = rec
        with ProxyFixture(backends, strategy="round_robin") as fx:
            picked = [header(request(fx.port)[1], "X-Backend-Name") for _ in range(6)]

        self.assertEqual(picked, ["A", "B", "C", "A", "B", "C"])
        self.assertEqual([len(records[n]) for n in names], [2, 2, 2])

    def test_weighted_strategy_routes_by_weight(self):
        heavy, _ = self.start_backend("heavy", weight=3)
        light, _ = self.start_backend("light", weight=1)
        with ProxyFixture([heavy, light], strategy="weighted_round_robin") as fx:
            picked = [header(request(fx.port)[1], "X-Backend-Name") for _ in range(8)]
        self.assertEqual(picked.count("heavy"), 6)
        self.assertEqual(picked.count("light"), 2)

    def test_unhealthy_backend_gets_no_traffic(self):
        a, _ = self.start_backend("A")
        b, b_records = self.start_backend("B")
        b.healthy = False
        with ProxyFixture([a, b]) as fx:
            picked = {header(request(fx.port)[1], "X-Backend-Name") for _ in range(4)}
        self.assertEqual(picked, {"A"})
        self.assertEqual(len(b_records), 0)


class TestAccessLog(BackendTestCase):
    """访问日志：内容、重试计数，以及写失败时的容错。

    注意每个用例都要等日志落到列表里再断言。访问日志是在响应写完之后
    才记的（nginx 的 access_log 也是这个时机），所以客户端拿到响应时，
    日志可能还没写——直接断言会变成随机失败的竞态。
    """

    def start_proxy(self, backends, logger, **config_kwargs) -> int:
        """起一个带指定访问日志器的代理，返回端口。"""
        config = ProxyConfig(listen_host="127.0.0.1", listen_port=0,
                             backends=backends, log_stdout=False, **config_kwargs)
        config.health_check.enabled = False
        server = create_server(config, access_logger=logger)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def wait_for_log(self, captured, count: int = 1) -> None:
        self.assertTrue(wait_for(lambda: len(captured) >= count, timeout=10.0),
                        f"等不到访问日志，实际只有 {len(captured)} 条")

    def test_logger_receives_structured_record(self):
        captured = []
        backend, _ = self.start_backend("A")
        port = self.start_proxy([backend], captured.append)

        request(port, "/logged?x=1")
        self.wait_for_log(captured)

        record = captured[0]
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/logged?x=1")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["backend"], backend.address)
        self.assertEqual(record["retries"], 0)
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertGreater(record["bytes"], 0)
        self.assertEqual(record["client"], "127.0.0.1")

    def test_logger_records_retry_count(self):
        captured = []
        bad, _ = self.start_backend("bad", fail_times=99)
        good, _ = self.start_backend("good")
        port = self.start_proxy([bad, good], captured.append, max_retries=2)

        request(port)
        self.wait_for_log(captured)

        self.assertEqual(captured[0]["retries"], 1)
        self.assertEqual(captured[0]["backend"], good.address)
        self.assertEqual(captured[0]["status"], 200)

    def test_logger_records_error_status(self):
        captured = []
        dead = Backend("127.0.0.1", free_port())
        port = self.start_proxy([dead], captured.append)

        request(port)
        self.wait_for_log(captured)

        self.assertEqual(captured[0]["status"], 502)
        self.assertEqual(captured[0]["backend"], dead.address)

    def test_client_ip_recorded(self):
        captured = []
        backend, _ = self.start_backend("A")
        port = self.start_proxy([backend], captured.append)

        request(port)
        self.wait_for_log(captured)

        self.assertEqual(captured[0]["client"], "127.0.0.1")

    def test_logger_failure_does_not_break_request(self):
        """写日志失败不能把正常请求带崩。"""
        def broken_logger(_record):
            raise RuntimeError("磁盘满了")

        backend, _ = self.start_backend("A")
        port = self.start_proxy([backend], broken_logger)

        status, _, _ = request(port)
        self.assertEqual(status, 200)


class TestAccessLoggerFile(unittest.TestCase):

    def test_writes_and_formats(self):
        handle, path = tempfile.mkstemp(suffix=".log")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        logger = AccessLogger(path, to_stdout=False)
        try:
            logger({
                "client": "127.0.0.1", "method": "GET", "path": "/a",
                "status": 200, "backend": "10.0.0.1:80",
                "bytes": 17, "duration_ms": 12.5, "retries": 0,
            })
        finally:
            logger.close()

        with open(path, encoding="utf-8") as f:
            line = f.read().strip()
        self.assertIn("127.0.0.1", line)
        self.assertIn("GET /a", line)
        self.assertIn("200", line)
        self.assertIn("10.0.0.1:80", line)
        self.assertEqual(len(line.split(" | ")), 8)

    def test_appends_across_sessions(self):
        handle, path = tempfile.mkstemp(suffix=".log")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        record = {"client": "c", "method": "GET", "path": "/", "status": 200,
                  "backend": "b", "bytes": 1, "duration_ms": 0.0, "retries": 0}
        for _ in range(2):
            logger = AccessLogger(path, to_stdout=False)
            logger(record)
            logger.close()

        with open(path, encoding="utf-8") as f:
            self.assertEqual(len([l for l in f if l.strip()]), 2)

    def test_creates_parent_directory(self):
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        path = os.path.join(base, "nested", "deep", "access.log")

        logger = AccessLogger(path, to_stdout=False)
        logger.close()
        self.assertTrue(os.path.exists(path))


class TestUnreadBodyDoesNotPoisonTheConnection(BackendTestCase):
    """正文没读完就必须断开。

    这是一类容易被忽略、但症状很怪的 bug：代理在读请求体**之前**就把请求
    拒了（限流、管理页、正文非法），正文于是留在套接字缓冲区里。HTTP/1.1
    默认长连接，客户端接着在这条连接上发下一个请求时，服务端从缓冲区读到
    的请求行就变成了 ``AAAA...GET /second``——回一个 501，客户端完全不知道
    自己那条请求为什么没了。

    所以这里不用 http.client（它会掩盖细节），而是手工在**同一条连接**上
    连发两个请求，直接看第二个还能不能正常拿到响应。
    """

    def exchange(self, port, first: bytes, second: bytes) -> tuple:
        """在一条连接上先后发两条裸报文，返回 (第一条, 第二条)。"""
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(first)
            time.sleep(0.3)
            first_reply = self._drain(sock)
            sock.sendall(second)
            time.sleep(0.3)
            return first_reply, self._drain(sock)
        finally:
            sock.close()

    @staticmethod
    def _drain(sock) -> bytes:
        """把当前可读到的字节都取出来（不阻塞等待更多）。"""
        sock.settimeout(0.5)
        buf = b""
        try:
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                buf += piece
        except (socket.timeout, OSError):
            pass
        return buf

    def test_post_to_admin_page_does_not_break_the_next_request(self):
        """带着正文访问管理页，服务端要断开，而不是留下正文污染下一个请求。

        期望的结果是「连接被干净地关掉」：客户端会在新连接上重发，而不是
        在旧连接上拿到一个 501。所以判据是「没有 501」，而不是「第二条
        请求还能成功」——后者只在不断连的前提下才成立，而这里断连才是对的。
        """
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            first, second = self.exchange(
                fx.port,
                b"POST /__admin HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 20\r\n\r\n" + b"A" * 20,
                b"GET /second HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertIn(b"200 OK", first.split(b"\r\n")[0])
        self.assertIn(b"Connection: close", first)
        # 关键断言：残留的 20 个 A 没有被当成下一个请求的请求行
        self.assertNotIn(b"501", first + second)
        self.assertNotIn(b"Unsupported method", first + second)
        self.assertEqual(records, [])

    def test_client_can_retry_on_a_fresh_connection(self):
        """断连的代价必须只是「重连一次」，业务要能正常继续。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            request(fx.port, "/__admin", method="POST", body=b"A" * 20)
            status, _, body = request(fx.port, "/after")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual([r["path"] for r in records], ["/after"])

    def test_rejection_announces_the_close_so_the_client_knows(self):
        """断连必须说出来，否则客户端会在一条要关的连接上再发一个请求。"""
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            first, _ = self.exchange(
                fx.port,
                b"POST /__admin HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 5\r\n\r\nhello",
                b"",
            )
        self.assertIn(b"Connection: close", first)

    def test_rejection_response_survives_the_close(self):
        """拒绝一个请求，不能连「为什么被拒绝」也一起拒掉。

        正文若比请求头**晚一步**到达（真实客户端常这样：先发头再发体），
        它就留在内核接收缓冲区里，而 rfile 的缓冲区是空的。此时直接 close()，
        Windows 发出的不是 FIN 而是 RST —— RST 会把客户端**已经收到、还没来得及
        读走**的响应一起冲掉，客户端拿到 ECONNABORTED，我们刚写出去的那个
        200 等于白写。客户端于是永远不知道限流/拒绝的理由，也就无从遵守。

        所以关闭前要先把残留正文读掉（见 proxy/server.py 的 DRAIN_LIMIT）。
        这条用例是确定性的：正文刻意等到请求头之后 50ms 才发，不靠时序碰运气。
        """
        backend, _ = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            reply = self.split_arrival(
                fx.port,
                b"POST /__admin HTTP/1.1\r\nHost: x\r\nContent-Length: 20\r\n\r\n",
                b"A" * 20,
            )
        self.assertIn(b"200 OK", reply)
        self.assertIn(b"Connection: close", reply)

    def split_arrival(self, port: int, head: bytes, body: bytes,
                      gap: float = 0.05) -> bytes:
        """请求头先发、正文隔一会儿再发，返回客户端读到的回复。"""
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(head)
            time.sleep(gap)     # 让代理先把头解析完、把响应写出来
            sock.sendall(body)
            return self._drain(sock)
        finally:
            sock.close()

    def test_bodyless_request_keeps_the_connection_alive(self):
        """没有正文就没有污染风险，长连接不该被无差别牺牲掉。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            first, second = self.exchange(
                fx.port,
                b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n",
                b"GET /two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertNotIn(b"Connection: close", first)
        self.assertEqual([r["path"] for r in records], ["/one", "/two"])

    def test_oversized_body_is_rejected_without_reading_it(self):
        """超大正文必须拒绝，而且不能真去读完——那等于替攻击者把活干了。"""
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            status, headers, body = request(
                fx.port, "/upload", method="POST", body=b"x" * 64,
                headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
            )
        self.assertEqual(status, 400)
        self.assertIn("请求体过大", body.decode("utf-8"))
        self.assertEqual(header(headers, "Connection"), "close")

    def test_bad_content_length_closes_the_connection(self):
        backend, records = self.start_backend("A")
        with ProxyFixture([backend]) as fx:
            first, second = self.exchange(
                fx.port,
                b"POST /upload HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: 5\r\n\r\nhello",
                b"GET /after HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        # 这条报文本身合法，应该被正常转发；这里验证的是长度非法的那条
        self.assertIn(b"200 OK", first.split(b"\r\n")[0])

        with ProxyFixture([backend]) as fx:
            first, second = self.exchange(
                fx.port,
                b"POST /upload HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: abc\r\n\r\n",
                b"GET /after HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
            )
        self.assertIn(b"400", first.split(b"\r\n")[0])
        self.assertIn(b"Connection: close", first)


class TestClientDisconnectsAreNotErrors(unittest.TestCase):
    """客户端断开是常态，不该在日志里升级成 ERROR 堆栈。

    代理的日志是给运维看的。如果每有一个客户端提前走开就喷二十行堆栈，
    真出问题时那一片噪声里就什么都找不出来了。

    这里捕的必须是 `ConnectionError` 这个**父类**，而不是逐个列子类——
    `ConnectionResetError`（10054）、`ConnectionAbortedError`（10053）、
    `BrokenPipeError` 说的是同一件事，列子类就会漏。实测就漏过
    `ConnectionAbortedError`：Windows 上一个普通断连打出了整页堆栈。
    """

    def levels_for(self, exc: BaseException):
        """把异常喂给 handle_error，返回它记下的日志级别列表。"""
        server = create_server(ProxyConfig(
            listen_host="127.0.0.1", listen_port=0, log_stdout=False,
            backends=[Backend("127.0.0.1", 1)],
        ))
        self.addCleanup(server.server_close)

        try:
            raise exc
        except type(exc):
            with self.assertLogs("proxy.server", level="DEBUG") as captured:
                server.handle_error(None, ("127.0.0.1", 1234))
        return [record.levelno for record in captured.records]

    def test_every_kind_of_connection_error_is_quiet(self):
        for exc in (
            ConnectionResetError(10054, "连接被对端重置"),
            ConnectionAbortedError(10053, "软件中止了一个已建立的连接"),
            BrokenPipeError(32, "管道已断开"),
            socket.timeout("读超时"),
        ):
            with self.subTest(type(exc).__name__):
                levels = self.levels_for(exc)
                noisy = [level for level in levels if level >= logging.ERROR]
                self.assertFalse(
                    noisy, f"{type(exc).__name__} 被当成错误打了出来"
                )

    def test_a_real_bug_is_still_reported_as_an_error(self):
        """别把过滤放宽成什么都吞——真异常必须留痕。"""
        levels = self.levels_for(ValueError("这是个真 bug"))
        self.assertTrue([level for level in levels if level >= logging.ERROR])


if __name__ == "__main__":
    unittest.main()
