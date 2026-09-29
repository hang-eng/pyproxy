"""HTTP 反向代理的转发核心。

一个请求的生命周期：

    客户端 --HTTP--> [本模块] --HTTP--> 后端 --HTTP--> [本模块] --> 客户端

三件事必须做对，否则代理「能跑但不对」：

1. **剥离逐跳首部（hop-by-hop headers）**。这些首部只在「一段」TCP 连接上
   有意义，不能被转发到下一段。RFC 7230 §6.1 列了 8 个：Connection、
   Keep-Alive、Proxy-Authenticate、Proxy-Authorization、TE、Trailer、
   Transfer-Encoding、Upgrade。此外 Connection 首部里点名的任何字段
   （如 ``Connection: X-Custom``）同样必须剥掉——否则客户端可以用它
   把本该被代理吃掉的字段偷渡给后端。

2. **重新分帧（re-framing）**。客户端可能用 Content-Length 发请求，
   也可能用 chunked；后端可能用 Content-Length 回应，也可能用 chunked。
   http.client 会把 chunked 解成裸字节，所以转发时必须由代理自己
   重新声明 Content-Length，不能沿用后端那套分帧信息。

3. **补 X-Forwarded-\\***。后端只能看到代理的 IP，原始客户端信息要由代理
   带过去，否则后端的访问日志和「按 IP 限流」全都会失效。

**失败重试的三条约束**（`_proxy` 里的循环）：

1. **非幂等方法绝不重试**。POST/PATCH 的语义是「创建」「下单」「支付」，
   后端可能已经把活干完了才断开连接；此时重试会真的产生第二笔订单。
   只有幂等方法（GET/HEAD/OPTIONS/PUT/DELETE）才进重试循环。
2. **重试必须换一台后端**。同一台机器挂了的话，在原机器上重试多少次都
   没用，只是把超时时间叠加起来。已试过的地址放进 exclude 集合传给
   balancer，全都试过就放弃。
3. **响应一旦开始回写就不能重试**。首部已经发给客户端了，再发一条响应
   只会让客户端收到两条拼在一起的报文。

重试之间不睡退避（backoff）：换后端重试是「另一台机器可能还活着」，
等一会儿并不会让它更可能活过来，只会白白增加客户端看到的延迟。

已知取舍（见 README「已知限制」）：
* 每个请求新建一条到后端的 TCP 连接，没有做连接池。实现简单、无
  陈旧连接问题，代价是每次请求多一次三次握手。生产级代理应做池化。
* 响应正文整体缓冲在内存中（无 Content-Length 的响应必须如此，
  否则算不出长度），因此不适合代理大文件下载。
* 后端自己返回的 5xx **不触发重试**：那是后端正常处理后的业务结论，
  换台机器重放未必得到相同结果。重试只针对连接失败与超时这类
  「请求可能根本没被处理」的故障。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .admin import is_admin_path, is_local_client, render as render_admin
from .balancer import Balancer, create_balancer
from .config import Backend, ProxyConfig

LOG = logging.getLogger("proxy.server")

# RFC 7230 §6.1 的逐跳首部，绝不转发
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# 这些状态码按定义不带响应正文
BODYLESS_STATUSES = frozenset({204, 304})

# 幂等方法：重复执行一次与执行一次的效果相同，因此失败后可以安全重试。
# GET/HEAD/OPTIONS 只读；PUT/DELETE 是幂等的（RFC 7231 §4.2.2）——
# 连发两次 DELETE 的结果与发一次相同。
#
# POST 和 PATCH 不在此列，**绝不重试**：POST 通常意味着「创建」「下单」
# 「支付」，后端可能已经处理完才断开连接，重试会真的产生第二笔订单。
# 这是本代理最重要的一条安全约束。
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# 请求体大小上限。代理要把请求体读进内存才能重试和重算 Content-Length，
# 所以必须设个上限，否则一个大文件上传就能把进程撑爆。
MAX_BODY_BYTES = 64 * 1024 * 1024

# 关闭连接前最多丢弃多少残留正文，以及愿意为它等多久。
#
# 为什么必须丢：Windows（以及多数协议栈）在「接收缓冲区里还有没读走的数据」
# 时 close()，发出的不是 FIN 而是 RST。RST 会连带把客户端**已经收到、但还
# 没被应用读走**的响应一起丢掉，客户端拿到的是 ECONNABORTED —— 我们刚发出去
# 的那个 429/404 等于白发。也就是：拒绝一个请求的时候，把「为什么被拒绝」
# 也一起拒了。
#
# 为什么必须有上限：客户端可以声称 Content-Length: 1GB 然后慢慢发，无上限地
# 读就是把它请进门做 DoS。超过上限就认了 —— 那种情况下缓冲区反正清不空，
# RST 躲不掉，不如别浪费这 0.5 秒。
DRAIN_LIMIT = 64 * 1024
DRAIN_TIMEOUT = 0.5

# 单次向客户端写出的分片大小，避免把整个响应拼成一个 bytes
CHUNK_SIZE = 64 * 1024

# 读客户端请求的超时（秒）。只保护「读客户端」这一段，
# 到后端的超时是另一套，由 config.timeout 控制。
CLIENT_TIMEOUT = 65


def connection_tokens(headers: Any) -> set:
    """取出 Connection 首部点名的所有字段名（小写）。

    例如 ``Connection: keep-alive, X-Custom-Hop`` -> {"keep-alive", "x-custom-hop"}
    """
    tokens = set()
    for value in headers.get_all("Connection", []) or []:
        for token in value.split(","):
            token = token.strip().lower()
            if token:
                tokens.add(token)
    return tokens


class BadRequest(Exception):
    """客户端请求本身有问题，回 400。"""


class UpstreamError(Exception):
    """到后端的转发失败，回 502/504。"""

    def __init__(self, status: int, reason: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail


class ProxyContext:
    """转发过程中需要的运行时依赖，统一挂在 server 实例上供 Handler 取用。"""

    def __init__(
        self,
        config: ProxyConfig,
        balancer: Balancer,
        health_checker: Any = None,
        access_logger: Optional[Callable[[Dict], None]] = None,
        rate_limiter: Any = None,
    ):
        self.config = config
        self.balancer = balancer
        self.health_checker = health_checker
        self.access_logger = access_logger
        self.rate_limiter = rate_limiter
        self.started_at = time.time()

        # 累计统计，只给 Web 管理界面看，不参与转发决策
        self._stats_lock = threading.Lock()
        self._total_requests = 0
        self._total_bytes = 0
        self._total_retries = 0
        self._status_classes: Counter = Counter()

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    def observe(self, record: Dict) -> None:
        """把一条访问记录累计进统计。

        独立于访问日志：日志可以关掉或写失败，管理页的数字不该跟着丢。
        """
        status = record.get("status") or 0
        with self._stats_lock:
            self._total_requests += 1
            self._total_bytes += record.get("bytes") or 0
            self._total_retries += record.get("retries") or 0
            if status:
                self._status_classes[status // 100] += 1

    def stats(self) -> Dict:
        with self._stats_lock:
            return {
                "total_requests": self._total_requests,
                "total_bytes": self._total_bytes,
                "total_retries": self._total_retries,
                "status_4xx": self._status_classes.get(4, 0),
                "status_5xx": self._status_classes.get(5, 0),
            }


class ProxyHandler(BaseHTTPRequestHandler):
    """把收到的请求转发给后端，把后端的响应原样回给客户端。"""

    # HTTP/1.1 才能支持长连接；代价是每个响应都必须有明确的分帧
    # （Content-Length 或 chunked），所以下面每一条出口都带 Content-Length。
    protocol_version = "HTTP/1.1"

    server_version = "MiniProxy/0.1"
    sys_version = ""  # 不把 Python 版本号暴露给客户端
    timeout = CLIENT_TIMEOUT

    # 单次请求的记账状态，默认值放在类上（见 _reset_state）
    _response_started = False
    _response_status = 0
    _response_bytes = 0
    _reframed = False
    # 请求正文是否已被完整读走。没读走的正文会留在连接缓冲区里，
    # 长连接下会污染下一个请求（见 _has_unread_body）。
    _body_consumed = False
    # 这一条请求不计入统计。管理页每 2 秒自刷新一次，计进去就成了
    # 「自己盯着自己看，所以请求数一直涨」的假数据。
    # 只对**成功渲染的管理页**置位：远程客户端探测 /__admin 拿到的 404
    # 要正常计数，那才是需要被看见的事。
    _stats_skipped = False

    # ------------------------------------------------------------------ 请求体

    def _read_request_body(self) -> bytes:
        """把请求体完整读进内存。

        必须读完：既要重算 Content-Length，也要留着重试时能重发。
        """
        self._reframed = False

        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            # 客户端用分块编码发送，解出来之后由我们重新声明长度
            self._reframed = True
            body = self._read_chunked_body()
            self._body_consumed = True
            return body

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._body_consumed = True   # 没有正文，等价于已经读完
            return b""

        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise BadRequest(f"Content-Length 不是整数：{raw_length!r}") from None
        if length < 0:
            raise BadRequest(f"Content-Length 为负数：{length}")
        if length == 0:
            self._body_consumed = True
            return b""
        if length > MAX_BODY_BYTES:
            # 故意不读：要防的就是"客户端拿一个巨大的正文把代理撑爆"，
            # 真去读完再拒绝等于自己把攻击做了
            raise BadRequest(
                f"请求体过大：{length} 字节（上限 {MAX_BODY_BYTES} 字节）"
            )

        data = self.rfile.read(length)
        if len(data) != length:
            raise BadRequest("请求体不完整，客户端可能提前断开")
        self._body_consumed = True
        return data

    def _read_chunked_body(self) -> bytes:
        """解开 chunked 请求体。

        BaseHTTPRequestHandler 只解析请求行和首部，不碰正文的分块编码，
        所以这里得自己按 RFC 7230 §4.1 的格式读：

            <长度(十六进制)>[;扩展]\r\n<数据>\r\n ... 0\r\n[trailer]\r\n
        """
        chunks: List[bytes] = []
        total = 0

        while True:
            line = self.rfile.readline(65536)
            if not line:
                raise BadRequest("分块请求体意外结束（缺少长度行）")

            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise BadRequest(f"非法的分块长度：{size_text!r}") from None

            if size == 0:
                # 末块之后可能跟 trailer 首部，一直读到空行为止
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break

            total += size
            if total > MAX_BODY_BYTES:
                raise BadRequest(f"分块请求体过大（上限 {MAX_BODY_BYTES} 字节）")

            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise BadRequest("分块请求体不完整")
            chunks.append(chunk)
            self.rfile.read(2)  # 每块结尾的 CRLF

        return b"".join(chunks)

    # ------------------------------------------------------------------ 转发

    def _upstream_path(self) -> str:
        """拿到该发给后端的请求目标（origin-form，即 /path?query）。"""
        path = self.path or "/"
        # 代理按 RFC 必须接受 absolute-form（GET http://host/path），
        # 转发给后端时要剥掉 scheme 和 authority。
        for scheme in ("http://", "https://"):
            if path.lower().startswith(scheme):
                rest = path[len(scheme):]
                slash = rest.find("/")
                return rest[slash:] if slash != -1 else "/"
        return path

    def _forwarded_for(self) -> str:
        client_ip = self.client_address[0] if self.client_address else "unknown"
        existing = self.headers.get("X-Forwarded-For")
        # 追加而不是覆盖：链路前面可能还有别的代理，覆盖会丢掉原始客户端
        return f"{existing}, {client_ip}" if existing else client_ip

    def _scheme(self) -> str:
        """客户端到代理这一段用的协议。

        本代理只监听明文 HTTP。将来若加上 TLS 终止，这里改成
        ``isinstance(self.connection, ssl.SSLSocket)`` 判断即可。
        """
        return "http"

    def _upstream_headers(self, body: bytes) -> List[Tuple[str, str]]:
        """构造发给后端的首部。

        返回列表而不是字典，是为了保留重复首部（如多个同名 Cookie）。
        """
        skip = connection_tokens(self.headers)
        headers: List[Tuple[str, str]] = []
        has_content_length = False

        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in skip:
                continue  # 逐跳首部不转发
            if lowered == "content-length":
                if self._reframed:
                    continue  # 分块已解开，旧的 Content-Length 已失效
                has_content_length = True
            headers.append((key, value))

        if self._reframed:
            headers.append(("Content-Length", str(len(body))))
            has_content_length = True
        elif body and not has_content_length:
            headers.append(("Content-Length", str(len(body))))

        # 原始客户端信息，后端靠这几个头还原真实来源
        headers.append(("X-Forwarded-For", self._forwarded_for()))
        host = self.headers.get("Host")
        if host:
            headers.append(("X-Forwarded-Host", host))
        headers.append(("X-Forwarded-Proto", self._scheme()))

        # 不复用到后端的连接：本代理每个请求新建一条连接，用完即关
        headers.append(("Connection", "close"))
        return headers

    def _forward(self, backend: Backend, body: bytes) -> None:
        """把当前请求发给 backend，并把响应回写给客户端。"""
        config = self.server.ctx.config
        conn = http.client.HTTPConnection(
            backend.host, backend.port, timeout=config.timeout
        )
        try:
            # skip_host: Host 由我们按原样转发，不让 http.client 用后端的
            #            host:port 覆盖掉
            # skip_accept_encoding: 透传客户端的 Accept-Encoding，不注入 identity
            conn.putrequest(
                self.command, self._upstream_path(),
                skip_host=True, skip_accept_encoding=True,
            )
            for key, value in self._upstream_headers(body):
                conn.putheader(key, value)
            conn.endheaders(body if body else None)

            response = conn.getresponse()
            self._relay_response(response)

        except socket.timeout as exc:
            # socket.timeout 是 OSError 的子类，必须先于 OSError 捕获
            raise UpstreamError(
                504, "Gateway Timeout",
                f"后端 {backend.address} 在 {config.timeout}s 内没有响应",
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError(
                502, "Bad Gateway", f"无法连接后端 {backend.address}：{exc}"
            ) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------ 回写

    def _response_has_body(self, status: int) -> bool:
        if self.command == "HEAD":
            return False
        if status in BODYLESS_STATUSES or 100 <= status < 200:
            return False
        return True

    def _relay_response(self, response: http.client.HTTPResponse) -> None:
        """把后端响应回写给客户端。"""
        # 一拿到响应就立刻标记「响应已开始」，而不是等到 end_headers()。
        # 因为下面任何一步抛异常，首部都已经留在 _headers_buffer 里了；
        # 若此刻标记还是 False，重试逻辑会往同一个缓冲区再写一条状态行，
        # 客户端会收到两条拼在一起的响应。标记为 True 就等于宣告：
        # 这条请求已经没有重试的余地了。
        self._response_started = True

        status = response.status
        self._response_status = status
        self.send_response_only(status, response.reason)

        # 后端给的 Connection 可能点名了别的字段，一并剥掉
        skip = connection_tokens(response.headers)
        declared_length = None
        for key, value in response.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in skip:
                continue
            if lowered == "content-length":
                # 不直接沿用：后端可能用 chunked，解出来的实际长度只有我们知道
                declared_length = value
                continue
            self.send_header(key, value)

        self.send_header("Via", f"1.1 {self.server_version}")

        if not self._response_has_body(status):
            # HEAD 要把后端声明的长度透传出去（它描述的是 GET 会返回多少），
            # 204/304 按定义不带正文也不需要分帧
            if self.command == "HEAD" and declared_length is not None:
                self.send_header("Content-Length", declared_length)
            self.end_headers()
            return

        if declared_length is not None and declared_length.isdigit():
            # 长度已知，可以边读边发，不必整体缓冲
            length = int(declared_length)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self._stream(response, length)
            return

        # 后端没给长度（多半是 chunked）：只能读全再算长度
        data = response.read()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)
            self._response_bytes += len(data)

    def _stream(self, response: http.client.HTTPResponse, length: int) -> None:
        """按 Content-Length 分片转发响应正文。"""
        remaining = length
        while remaining > 0:
            chunk = response.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                # 后端提前关闭，正文比它声明的短。此时首部已经发出去了，
                # 改不了状态码，只能断开连接，否则客户端会把下一条响应的
                # 开头当成这一条的剩余正文。
                LOG.warning(
                    "后端响应正文不完整：声明 %d 字节，还差 %d 字节",
                    length, remaining,
                )
                self.close_connection = True
                return
            self.wfile.write(chunk)
            self._response_bytes += len(chunk)
            remaining -= len(chunk)

    def _send_error_json(
        self,
        status: int,
        reason: str,
        detail: str,
        backend: Optional[str] = None,
        extra_headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        """在转发失败时回一个 JSON 错误体。

        比空响应体或 HTML 错误页更容易被调用方解析。
        """
        self._response_status = status
        if self._response_started:
            # 首部已经发出，没法再改状态码了，只能断开
            LOG.warning("响应已开始，无法改回 %d：%s", status, detail)
            self.close_connection = True
            return

        payload = json.dumps(
            {
                "status": status,
                "error": reason,
                "detail": detail,
                "backend": backend,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response_only(status, reason)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Date", self.date_time_string())
        self.send_header("Server", self.server_version)
        for key, value in extra_headers or ():
            self.send_header(key, value)
        self._close_if_body_unread()
        # 要断开连接就得说出来。HTTP/1.1 默认长连接，如果不声明，
        # 客户端会认为这条连接还能用，在一条马上就要被服务端关掉的
        # 套接字上发下一个请求——上限流路径上这会表现为「被限流之后
        # 莫名其妙报一个连接错误」，比 429 本身更难排查。
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Via", f"1.1 {self.server_version}")
        self._response_started = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
            self._response_bytes += len(payload)

    # ------------------------------------------------------------ 管理页与限流

    def _serve_admin(self) -> bool:
        """处理 ``/__admin``。返回 True 表示请求已被这一页消化掉。"""
        if not is_admin_path(self._upstream_path()):
            return False

        if not is_local_client(self.client_address):
            # 假装这个路径不存在。回 403 等于告诉对方「这儿有东西」，
            # 反而给了探测的线索。
            LOG.info("拒绝来自 %s 的管理页访问", self.client_address)
            self._send_error_json(404, "Not Found", "没有这个路径")
            return True

        payload = render_admin(self.server.ctx, self.server.ctx.uptime)
        self._response_status = 200
        self._response_started = True
        self._stats_skipped = True
        # 管理页从来不看正文。客户端要是带着正文来（POST /__admin），
        # 正文就还留在连接里，必须断开，否则下一个请求会被它污染。
        self._close_if_body_unread()
        self.send_response_only(200, "OK")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # 页面每 2 秒自刷新，中间那一层缓存住了就看不到实时状态了
        self.send_header("Cache-Control", "no-store")
        self.send_header("Server", self.server_version)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
            self._response_bytes += len(payload)
        return True

    def _reject_if_rate_limited(self) -> bool:
        """超过限流阈值就回 429。返回 True 表示请求已被拒绝。"""
        limiter = self.server.ctx.rate_limiter
        if limiter is None or not limiter.enabled:
            return False

        client_ip = self.client_address[0] if self.client_address else "-"
        allowed, retry_after = limiter.check(client_ip)
        if allowed:
            return False

        # 刻意在**读请求体之前**拒绝：已经决定不处理了，再花带宽把
        # 一大坨 body 读进内存毫无意义（这本身就是限流要防的东西）。
        # 代价是正文留在连接里，必须断开——但这个判断交给
        # _close_if_body_unread 按「是否真有正文」来做：
        # 浏览器狂刷 GET 被限流时没有正文，长连接该保留就保留，
        # 不能为了一个边界情况把所有连接复用都牺牲掉。
        self._send_error_json(
            429,
            "Too Many Requests",
            f"客户端 {client_ip} 请求过于频繁，请稍后重试",
            extra_headers=[("Retry-After", str(max(1, int(retry_after + 0.999))))],
        )
        return True

    # ------------------------------------------------------------------ 入口

    def _retryable(self) -> bool:
        """这个请求失败后能不能换个后端重试。"""
        return self.command in IDEMPOTENT_METHODS

    def _reset_state(self) -> None:
        """重置单次请求的记账状态。

        类属性上也有同名默认值，双保险：任何一条出口路径（包括还没走到
        初始化就出错的分支）都不会因为属性没赋值而抛 AttributeError。
        """
        self._response_started = False
        self._response_status = 0
        self._response_bytes = 0
        self._reframed = False
        self._body_consumed = False
        self._stats_skipped = False

    def _has_unread_body(self) -> bool:
        """这条请求的正文有没有可能还留在连接里没被读走。

        凡是「读正文之前就把请求拒掉」的路径（限流、管理页、正文非法），
        正文就还在套接字缓冲区里。HTTP/1.1 默认长连接，不主动断开的话，
        这些残留字节会被当成下一个请求的开头——实测的现象是请求行变成
        ``AAAAAAAAAAAAAAAAAAAAGET /second``，服务端回一个 501，而客户端
        完全不知道自己那条请求为什么没了。

        这是同一类 bug 的第三个出口（前两个是限流拒绝和响应已开始回写），
        所以做成一个统一的判断，而不是在三处各补一行。
        """
        if self._body_consumed:
            return False
        # finish() 里也会问一次，而那时请求未必解析成功过（比如探测工具连上
        # 来一个字都不发就直接断开，parse_request 会在设置 headers 之前返回）
        headers = getattr(self, "headers", None)
        if not headers:
            return False
        if headers.get("Transfer-Encoding"):
            return True
        try:
            return int(headers.get("Content-Length") or 0) > 0
        except (TypeError, ValueError):
            return True  # 长度都读不出来，保守起见按「有正文」处理

    def _close_if_body_unread(self) -> None:
        """正文没读完就必须断开，否则下一个请求会被残留字节污染。

        没有正文的请求（绝大多数 GET）不受影响，长连接照常保留——
        不能为了一个边界情况把所有连接的复用都牺牲掉。

        断开的副作用（RST 冲掉自己的响应）由 `_drain_remaining_body` 兜住。
        """
        if self._has_unread_body():
            self.close_connection = True

    def _drain_remaining_body(self) -> None:
        """把没读走的正文丢掉，免得关闭连接时发出 RST。

        由来见 `DRAIN_LIMIT` 上方那段注释：不丢，客户端可能拿不到我们刚
        发出去的那个响应。
        """
        if not self._has_unread_body():
            return

        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > DRAIN_LIMIT:
            return  # 读不干净，RST 躲不掉，不白等

        # 分块编码看不出总长，按上限试探着读
        remaining = declared if declared > 0 else DRAIN_LIMIT

        original = self.connection.gettimeout()
        self.connection.settimeout(DRAIN_TIMEOUT)
        try:
            while remaining > 0:
                # 用 read1 而不是 read：拿到多少算多少。read 会为了凑满 n 字节
                # 一直等下去，而客户端发完就停手了——只会等到超时。
                chunk = self.rfile.read1(min(remaining, CHUNK_SIZE))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            # 超时、对端已经走了、缓冲区已关闭：都无所谓，本来就是要丢掉它
            pass
        finally:
            try:
                self.connection.settimeout(original)
            except OSError:
                pass

    def finish(self) -> None:
        """收尾：先把残留正文丢掉，再真正关闭连接。

        补在这里而不是各个拒绝分支里，是因为「关连接」只在这一个地方发生。
        补在分支上就又会退化成「每加一条提前返回的路径就重踩一遍」——
        正文污染那类 bug 已经踩过三次了。
        """
        self._drain_remaining_body()
        super().finish()

    def _proxy(self) -> None:
        """所有方法的统一入口：选后端 -> 转发 -> 失败则换后端重试。"""
        self._reset_state()
        started = time.monotonic()
        backend: Optional[Backend] = None
        retries = 0
        try:
            # 管理页与限流都拦在「读请求体」之前：这两条路都不需要 body，
            # 先把可能很大的一坨数据读进内存再拒绝，纯属白费——
            # 而「白费带宽和内存」恰恰是限流本身要防的东西。
            if self._serve_admin():
                return
            if self._reject_if_rate_limited():
                return

            try:
                body = self._read_request_body()
            except BadRequest as exc:
                self._send_error_json(400, "Bad Request", str(exc))
                return

            ctx = self.server.ctx
            tried: Set[str] = set()
            last_error: Optional[UpstreamError] = None
            last_backend: Optional[str] = None
            attempts = 0

            while True:
                candidate = ctx.balancer.select(exclude=tried or None)
                if candidate is None:
                    # 没有可选后端：要么一开始就没有健康的，要么全都试过了
                    break

                if attempts:
                    retries += 1
                    LOG.warning(
                        "后端 %s 转发失败，换 %s 重试（第 %d 次）",
                        last_backend, candidate.address, retries,
                    )
                attempts += 1
                backend = candidate
                tried.add(backend.address)

                try:
                    self._forward(backend, body)
                    last_error = None
                    break
                except UpstreamError as exc:
                    last_error = exc
                    last_backend = backend.address

                    # 响应已经开始回写给客户端，没有重试的余地了：
                    # 再发一条响应只会让客户端收到两条拼在一起的报文
                    if self._response_started:
                        LOG.warning(
                            "响应已部分发出，不再重试：%s %s",
                            self.command, self.path,
                        )
                        break
                    # 非幂等方法（POST/PATCH）绝不重试
                    if not self._retryable():
                        LOG.info("非幂等方法 %s 不重试：%s", self.command, self.path)
                        break
                    # attempts 已经是「已发起的次数」，上限 1 + max_retries
                    if attempts > ctx.config.max_retries:
                        break

            if last_error is not None:
                self._send_error_json(
                    last_error.status, last_error.reason, last_error.detail,
                    backend=backend.address if backend else None,
                )
            elif backend is None:
                self._send_error_json(
                    503, "Service Unavailable",
                    "所有后端都不可用（健康检查剔除、已被全部重试过，或未配置）",
                )

        except (BrokenPipeError, ConnectionResetError):
            # 客户端等不及先走了，不是错误
            LOG.debug("客户端断开：%s %s", self.command, self.path)
        except Exception:
            LOG.exception("转发 %s %s 时出现未预期的异常", self.command, self.path)
            self._send_error_json(500, "Internal Server Error", "代理内部异常")
        finally:
            self._log_access(backend, (time.monotonic() - started) * 1000.0, retries)

    def _log_access(
        self, backend: Optional[Backend], duration_ms: float, retries: int = 0
    ) -> None:
        record = {
            "client": self.client_address[0] if self.client_address else "-",
            "method": self.command,
            "path": self.path,
            "status": self._response_status,
            "backend": backend.address if backend else "-",
            "bytes": self._response_bytes,
            "duration_ms": duration_ms,
            "retries": retries,
        }
        logger = self.server.ctx.access_logger
        if logger is not None:
            try:
                logger(record)
            except Exception:
                LOG.exception("访问日志写入失败")
        else:
            LOG.info(
                "%s %s %s -> %s %s %.1fms",
                record["client"], record["method"], record["path"],
                record["backend"], record["status"], record["duration_ms"],
            )

        # 统计独立于日志：日志可以关掉、可以写失败，管理页上的数字不该
        # 跟着一起丢。
        if not self._stats_skipped:
            self.server.ctx.observe(record)

    def do_CONNECT(self) -> None:  # noqa: N802 - 方法名由 BaseHTTPRequestHandler 规定
        """CONNECT 是正向代理用来建隧道的，反向代理不支持。"""
        self._reset_state()
        started = time.monotonic()
        try:
            self._send_error_json(405, "Method Not Allowed", "反向代理不支持 CONNECT")
        finally:
            # 也要记进访问日志与统计。扫描器很爱发 CONNECT/TRACE，
            # 这类请求如果完全不留痕，「谁在扫我」就永远看不出来。
            self._log_access(None, (time.monotonic() - started) * 1000.0, 0)

    do_TRACE = do_CONNECT  # TRACE 会回显请求，常被用作 XST 攻击，不转发

    def log_message(self, fmt: str, *args: Any) -> None:
        """BaseHTTPRequestHandler 默认往 stderr 打日志，改走 logging。"""
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        LOG.warning("%s - %s", self.address_string(), fmt % args)


# 反向代理要支持的方法统一走 _proxy；
# BaseHTTPRequestHandler 靠 getattr(self, "do_" + command) 派发，
# 所以有多少个方法就要挂多少个 do_XXX。
for _method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
    setattr(ProxyHandler, f"do_{_method}", ProxyHandler._proxy)


class ProxyServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器。

    必须用 ThreadingHTTPServer 而不是 HTTPServer：后者单线程串行处理，
    一个慢请求就能把整个代理堵死——代理本身就是靠并发转发吃饭的。
    """

    daemon_threads = True        # 主线程退出时不被工作线程拖住
    allow_reuse_address = True   # 重启时不必等 TIME_WAIT

    def __init__(self, ctx: ProxyContext):
        self.ctx = ctx
        super().__init__(
            (ctx.config.listen_host, ctx.config.listen_port), ProxyHandler
        )

    def handle_error(self, request: Any, client_address: Any) -> None:
        """客户端断开是常态，别为此打一整页 traceback。

        这里捕 `ConnectionError` 这个**父类**，而不是逐个列它的子类：
        `ConnectionResetError` / `BrokenPipeError` / `ConnectionAbortedError`
        都是它的子类，三个说的是同一件事——对端没了。列子类就会漏掉下一个
        才想起来的那个：实测漏过 `ConnectionAbortedError`（Windows 上表现为
        WinError 10053），一个普通的客户端断连打出了二十行堆栈。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout)):
            LOG.debug("连接异常结束 %s：%s", client_address, exc)
            return
        LOG.exception("处理来自 %s 的连接时异常", client_address)


def create_server(
    config: ProxyConfig,
    balancer: Optional[Balancer] = None,
    health_checker: Any = None,
    access_logger: Optional[Callable[[Dict], None]] = None,
    rate_limiter: Any = None,
) -> ProxyServer:
    """按配置创建代理服务器（此时还没开始监听，需调用 serve_forever）。"""
    if balancer is None:
        balancer = create_balancer(config.strategy, config.backends)
    ctx = ProxyContext(
        config, balancer, health_checker, access_logger, rate_limiter
    )
    return ProxyServer(ctx)
