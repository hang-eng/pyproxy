"""模拟后端那一页的判定逻辑。

单独测它，是因为这一页承担着「**演示代理没偷懒**」的职责，一旦它说谎，
演示就变成了误导——比没有演示更糟。

真实踩过的坑：`Connection` 一开始和另外 4 个逐跳首部一样处理，只要后端
看到非空值就判「透传了！这是代理的 bug」。但逐跳首部的语义是**每段连接
各管各的**：代理到后端是全新的一段，代理**必须**替这一段自己声明
`Connection`（本代理写的是 `close`，一个请求一条连接）。所以后端永远会
看到它，而那是正确行为。于是页面在一个完全正确的实现上亮红灯。

判定改成看**内容**：代理自己只会写 `close` / `keep-alive`，出现别的词才
说明客户端发的东西活到了后端。
"""

from __future__ import annotations

import http.client
import threading
import unittest

from mock_backend import HOP_BY_HOP_PROBE, _audit_hop_by_hop, _hop_by_hop_rows, _render_page


def snapshot(**overrides) -> dict:
    """一份最小可渲染的快照，用 overrides 换掉要看的那部分。"""
    base = {
        "我是后端": "A",
        "方法": "GET",
        "路径": "/",
        "请求体": "",
        "真实客户端IP": "127.0.0.1",
        "原始Host": "127.0.0.1:8080",
        "后端实际看到的Host": "127.0.0.1:8080",
        "客户端协议": "http",
        "逐跳首部已剥离": {name: None for name in HOP_BY_HOP_PROBE},
    }
    base.update(overrides)
    return base


def verdicts(rows) -> dict:
    """把审计结果压成 {首部名: 结论}，方便断言。"""
    return {name: verdict for name, _shown, _tone, verdict in rows}


def tones(rows) -> dict:
    return {name: tone for name, _shown, tone, _verdict in rows}


class TestConnectionIsNotTreatedLikeTheOthers(unittest.TestCase):
    """这一组全是「不许误报」。"""

    def test_proxy_own_close_is_not_a_leak(self):
        rows, leaks = _audit_hop_by_hop(
            snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                          "Connection": "close"}})
        )
        self.assertEqual(leaks, [])
        self.assertEqual(tones(rows)["Connection"], "own")

    def test_keep_alive_as_a_connection_token_is_also_the_proxys_own(self):
        """将来若给上游加连接池，代理声明 keep-alive 同样合法，不该报警。"""
        _rows, leaks = _audit_hop_by_hop(
            snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                          "Connection": "keep-alive"}})
        )
        self.assertEqual(leaks, [])

    def test_missing_connection_is_not_reported_as_a_leak(self):
        """代理没声明 Connection 属于「实现变了」，不是泄漏。"""
        rows, leaks = _audit_hop_by_hop(snapshot())
        self.assertEqual(leaks, [])
        self.assertEqual(tones(rows)["Connection"], "dim")

    def test_case_and_spacing_do_not_matter(self):
        _rows, leaks = _audit_hop_by_hop(
            snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                          "Connection": " Close "}})
        )
        self.assertEqual(leaks, [])


class TestRealLeaksAreStillCaught(unittest.TestCase):
    """不能因为怕误报就把判定放松到抓不住真漏。"""

    def test_client_token_surviving_is_a_leak(self):
        rows, leaks = _audit_hop_by_hop(
            snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                          "Connection": "close, keep-alive, X-Custom-Hop"}})
        )
        self.assertEqual(tones(rows)["Connection"], "leaked")
        # 代理自己会写 close / keep-alive，所以它们不算数；可疑的只有客户端那个
        self.assertEqual(leaks, ["Connection: x-custom-hop"])

    def test_each_plain_hop_by_hop_header_is_caught(self):
        for name in HOP_BY_HOP_PROBE:
            if name == "Connection":
                continue
            with self.subTest(name):
                rows, leaks = _audit_hop_by_hop(
                    snapshot(**{"逐跳首部已剥离": {
                        **{n: None for n in HOP_BY_HOP_PROBE}, name: "whatever"}})
                )
                self.assertEqual(tones(rows)[name], "leaked")
                self.assertEqual(leaks, [f"{name}: whatever"])

    def test_a_clean_hop_by_hop_set_has_no_leaks(self):
        rows, leaks = _audit_hop_by_hop(
            snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                          "Connection": "close"}})
        )
        self.assertEqual(leaks, [])
        self.assertEqual(tones(rows)["Connection"], "own")
        for name in HOP_BY_HOP_PROBE:
            if name != "Connection":
                self.assertEqual(tones(rows)[name], "stripped", name)


class TestPageAndTableAgree(unittest.TestCase):
    """表格画绿的、首屏结论说红的，就是页面在自相矛盾。"""

    def test_clean_snapshot_says_nothing_leaked(self):
        snap = snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                            "Connection": "close"}})
        page = _render_page(snap, "MockBackend/1.0")
        self.assertNotIn("透传了", page)
        self.assertIn("该剥的逐跳首部全被代理剥掉了", page)
        self.assertEqual(page.count("已剥离"), 4)

    def test_leaking_snapshot_is_called_out_in_both_places(self):
        snap = snapshot(**{"逐跳首部已剥离": {
            **{n: None for n in HOP_BY_HOP_PROBE}, "Upgrade": "websocket"}})
        page = _render_page(snap, "MockBackend/1.0")
        self.assertIn("有逐跳首部漏过来了", page)
        self.assertIn("透传了", page)
        self.assertIn("Upgrade", page)

    def test_table_and_note_use_the_same_verdicts(self):
        """有 Connection 而没有别的噪声时，两处都不该提「漏」。"""
        snap = snapshot(**{"逐跳首部已剥离": {**{n: None for n in HOP_BY_HOP_PROBE},
                                            "Connection": "close"}})
        rows, _leaks = _audit_hop_by_hop(snap)
        table = _hop_by_hop_rows(rows)
        self.assertNotIn("透传了", table)
        self.assertIn("代理为自己那段连接生成的（正确）", table)

    def test_page_offers_a_runnable_reproduction(self):
        """页脚那条命令要能直接粘进终端，地址得是代理的真实地址。"""
        page = _render_page(snapshot(), "MockBackend/1.0")
        self.assertIn("curl -s -H", page)
        self.assertIn("http://127.0.0.1:8080/?format=json", page)

    def test_page_states_the_blind_spot(self):
        """客户端自己也发 Connection: close 时看不出来——这件事得写在页面上，"""
        page = _render_page(snapshot(), "MockBackend/1.0")
        self.assertIn("盲区", page)


class TestPageAgainstARealProxy(unittest.TestCase):
    """端到端：真代理 + 真模拟后端，页面上的结论必须是「干净」。

    上面的用例都是喂假快照。这一条才是真正防回归的——它把整条链路跑起来，
    如果哪天判定逻辑又把代理自己生成的 Connection 当成泄漏，这里会红。
    """

    count = 2

    def setUp(self):
        from mock_backend import start_backends
        from proxy.balancer import create_balancer
        from proxy.config import Backend, ProxyConfig
        from proxy.server import create_server

        self.servers, ports = start_backends(self.count, host="127.0.0.1")

        def stop_backends():
            for server in self.servers:
                server.shutdown()
                server.server_close()

        self.addCleanup(stop_backends)

        backends = [Backend("127.0.0.1", port, 1) for port in ports]
        config = ProxyConfig(listen_host="127.0.0.1", listen_port=0,
                             log_stdout=False, backends=backends)
        config.health_check.enabled = False

        self.proxy = create_server(
            config, balancer=create_balancer(config.strategy, backends)
        )
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def fetch(self, path: str, accept: str = None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.proxy.server_address[1], timeout=10
        )
        try:
            headers = {"Accept": accept} if accept else {}
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_correct_proxy_produces_no_leak_claims(self):
        status, body = self.fetch("/", accept="text/html")
        page = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertNotIn("透传了", page, "代理行为正确，页面却报了泄漏")
        self.assertEqual(page.count("已剥离"), 4)

    def test_json_keys_are_unchanged(self):
        """页面换皮不能动 JSON 契约——它是可被程序断言的那一份。"""
        import json

        status, body = self.fetch("/?format=json")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(payload),
            sorted(["我是后端", "方法", "路径", "请求体", "真实客户端IP", "原始Host",
                    "后端实际看到的Host", "客户端协议", "逐跳首部已剥离"]),
        )
        self.assertEqual(sorted(payload["逐跳首部已剥离"]), sorted(HOP_BY_HOP_PROBE))
        self.assertEqual(payload["逐跳首部已剥离"]["Connection"], "close")


if __name__ == "__main__":
    unittest.main()
