"""测试脚手架：模拟后端 + 起代理的工具函数。

单独成文件，是为了让四个测试模块共用同一套夹具，避免每个文件各写一份
「起个后端、起个代理」的样板代码——样板一多，改接口时就会漏改。

模拟后端是**可配置**的：能指定状态码、延迟、响应体、是否用分块编码、
以及「前 N 次请求直接断开连接」。最后这一项用来制造转发失败，从而测试
重试逻辑——这是整个代理里最容易写错、也最需要测试覆盖的一条路径。
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

from proxy.balancer import create_balancer
from proxy.config import Backend, ProxyConfig
from proxy.ratelimit import RateLimiter
from proxy.server import create_server


# --------------------------------------------------------------------------
# 端口与 HTTP 工具
# --------------------------------------------------------------------------

def free_port() -> int:
    """占一个端口再放掉，得到一个当前没人监听的端口。

    用来构造「连接被拒」的后端。注意不能直接用固定端口，否则一旦那个
    端口恰好被占，测试就会以完全无关的原因失败。
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port: int, path: str = "/", method: str = "GET",
            body: Any = None, headers: Dict[str, str] = None,
            timeout: float = 15.0, host: str = "127.0.0.1",
            ) -> Tuple[int, List[Tuple[str, str]], bytes]:
    """发一个请求，返回 (状态码, 首部列表, 响应体)。

    首部返回列表而不是字典：本代理会逐个转发重复首部（比如多个
    Set-Cookie），用字典会把它们合并掉，测试就失去意义了。

    host 可以换成非回环地址，用来模拟「远程客户端」——比如验证
    管理页对外不可见时，必须真的从另一个地址连过去，改不了客户端
    地址就等于没测。
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, list(response.getheaders()), response.read()
    finally:
        conn.close()


def raw_exchange(port: int, payload: bytes, timeout: float = 15.0) -> bytes:
    """直接把原始字节写进连接，返回服务端的原始回复。

    http.client 没法构造「重复首部」和「格式古怪的请求行」，
    需要验证这些场景时只能走裸 socket。
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout)
    try:
        sock.sendall(payload)
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        sock.close()


def header_values(headers: List[Tuple[str, str]], name: str) -> List[str]:
    """取出某个首部的所有取值（大小写不敏感）。"""
    wanted = name.lower()
    return [v for k, v in headers if k.lower() == wanted]


def header(headers: List[Tuple[str, str]], name: str):
    """取出某个首部的第一个取值，没有则返回 None。"""
    values = header_values(headers, name)
    return values[0] if values else None


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """轮询等待条件成立。

    健康检查之类的逻辑是异步的，用固定 time.sleep 会让测试要么不稳定、
    要么白等很久。轮询到就立刻返回。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def wait_for_stats(ctx, **expected):
    """等统计累加到位，返回最终快照。

    `observe()` 是在响应的 `finally` 里执行的，和 nginx 的 `access_log`
    同一时序——客户端**先**拿到响应，统计**后**落地。所以「发完请求立刻
    读统计」本质上是个竞态，偶尔会少一条。

    这不是 bug，是"日志在响应之后写"的固有顺序；要断言统计值就得等它
    追上来，而不是靠 sleep 碰运气。
    """
    def ready() -> bool:
        stats = ctx.stats()
        return all(stats.get(key) == value for key, value in expected.items())

    wait_for(ready, timeout=5.0)
    return ctx.stats()


# --------------------------------------------------------------------------
# 模拟后端
# --------------------------------------------------------------------------

class MockBackend(BaseHTTPRequestHandler):
    """可配置的模拟后端。

    子类通过类属性配置行为（见 ``make_backend``）：
        name            后端名，回显在 X-Backend-Name 上
        status          返回的状态码
        delay           响应前先睡多少秒
        body            响应体
        response_headers 额外响应首部（可以是重复的）
        chunked         是否用分块编码发送响应体
        fail_times      前 N 次请求不写响应、直接断开连接
        records         收到的请求记录（每条是 dict）
        requests_seen   收到过的请求总数
    """

    protocol_version = "HTTP/1.1"

    # 用一个显眼的自定义 Server 名，好让测试能区分「后端发的」和
    # 「代理自己加的」。sys_version 清空是为了不带上 Python 版本号。
    server_version = "MockBackend/1.0"
    sys_version = ""

    name = "?"
    status = 200
    delay = 0.0
    body = b"ok"
    response_headers: Tuple[Tuple[str, str], ...] = ()
    chunked = False
    fail_times = 0
    records: List[Dict[str, Any]] = []
    requests_seen = 0

    def log_message(self, *args) -> None:
        pass  # 交给代理去记日志，测试输出保持干净

    def _record(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        # 逐跳首部在代理侧就该被剥掉，这里原样记下来供断言
        entry = {
            "method": self.command,
            "path": self.path,
            "body": payload,
            "headers": list(self.headers.items()),
            "raw_pairs": [(k.lower(), v) for k, v in self.headers.items()],
        }
        cls = type(self)
        cls.requests_seen += 1
        cls.records.append(entry)
        return entry

    def _handle(self) -> None:
        cls = type(self)
        self._record()

        if cls.requests_seen <= cls.fail_times:
            # 一个字都不回就关连接：模拟后端进程崩溃/被防火墙丢包，
            # 代理应当把它变成 502 并（在允许时）换一台重试
            self.close_connection = True
            return

        if cls.delay:
            time.sleep(cls.delay)

        if cls.chunked:
            self.send_response(cls.status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            for key, value in cls.response_headers:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                # 拆成多段发，逼代理真的走一遍分块解码
                for piece in (cls.body[:1], cls.body[1:], b""):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            return

        self.send_response(cls.status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(cls.body)))
        self.send_header("X-Backend-Name", cls.name)
        for key, value in cls.response_headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cls.body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    def do_HEAD(self) -> None:
        self._handle()


class QuietServer(ThreadingHTTPServer):
    """不往 stderr 喷堆栈的 HTTP 服务器。

    模拟后端故意断连接时，socketserver 会调用 handle_error 打一堆
    traceback。那是测试**期望**发生的事，不该污染输出。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        pass


def make_backend(name: str, **options) -> Tuple[type, List[Dict[str, Any]]]:
    """造一个模拟后端类，返回 (类, 请求记录列表)。"""
    records: List[Dict[str, Any]] = []
    attrs: Dict[str, Any] = {
        "name": name,
        "records": records,
        "requests_seen": 0,
        "response_headers": (),
    }
    attrs.update(options)
    return type("Mock" + name, (MockBackend,), attrs), records


def start_server(handler_cls: type) -> QuietServer:
    """在随机端口上启动一个服务器，返回 server 对象。"""
    server = QuietServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# 代理夹具
# --------------------------------------------------------------------------

class ProxyFixture:
    """一个跑起来的代理 + 它的后端，退出时自动全部收尾。

    用法::

        with ProxyFixture([backend_a, backend_b]) as fx:
            status, headers, body = request(fx.port)
    """

    def __init__(self, backends: List[Backend], rate_limit=None, **config_kwargs):
        self.backends = list(backends)
        options = {
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "log_stdout": False,
            "backends": self.backends,
        }
        options.update(config_kwargs)
        self.config = ProxyConfig(**options)
        # 后台健康检查线程会和测试争抢后端状态，默认关掉；
        # 需要测健康检查的用 check_once() 手动驱动。
        self.config.health_check.enabled = False
        if rate_limit is not None:
            self.config.rate_limit = rate_limit
        # 总是装配一个限流器（配置里默认是关的）：管理页能测到"已关闭"
        # 与"已启用"两条分支，而不是"未装配"这条假路径。
        self.rate_limiter = RateLimiter(self.config.rate_limit)
        self.server = None
        self.port = 0
        self.balancer = None

    def __enter__(self) -> "ProxyFixture":
        self.balancer = create_balancer(self.config.strategy, self.backends)
        self.server = create_server(
            self.config, balancer=self.balancer, rate_limiter=self.rate_limiter
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.server_close()
        return False
