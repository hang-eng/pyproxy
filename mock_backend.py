"""一个可独立运行的模拟后端。

两个用途：

* 被 `demo.py` 拉起若干个，做成本机一键演示；
* 在 `docker-compose.yml` 里单独作为一个容器跑，用来演示多后端负载均衡。

把它单独成文件而不是塞进 `demo.py`，是因为容器里需要「一个进程就是一个
后端」——而 `demo.py` 的定位是「一个进程拉起全部」。两者的启动模型不同，
共用同一个 Handler 实现才不会两边逻辑漂移。

    python mock_backend.py --port 9001 --name A
"""

from __future__ import annotations

import argparse
import html
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 逐跳首部在这一层就该被代理剥掉，所以在响应里回显它们，一眼就能看出
# 代理有没有偷懒
HOP_BY_HOP_PROBE = ("Connection", "Keep-Alive", "Transfer-Encoding", "TE", "Upgrade")

# 代理替它到后端那一段连接生成 Connection 时可能用到的词。
#
# Connection 不能和另外 4 个一视同仁。「逐跳」的意思是：这个字段只在**一段**
# 连接上有意义，每段各管各的。代理到后端是新的一段，所以代理**必须**自己生成
# 一个 Connection 声明这段怎么用（本代理是 `close`：一个请求一条连接，用完即关）。
# 于是后端**永远**会看到 Connection —— 这恰恰说明代理在正常工作，而不是「漏了」。
# 把非空一律判成 bug，会冤枉一个正确的实现。
#
# 真正算漏的是：**客户端**发的东西活到了后端。所以不看有没有值，看内容里有没有
# 代理自己不会写的词。
_PROXY_OWN_CONNECTION_TOKENS = frozenset({"close", "keep-alive"})

_STYLE = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 30px 34px; background: #0f1419; color: #d8dee9;
  font: 14px/1.65 "Consolas", "Cascadia Mono", "Microsoft YaHei", monospace;
}
h1 { font-size: 20px; margin: 0 0 4px; color: #e8eef5; font-weight: 600; }
h1 .badge {
  display: inline-block; min-width: 30px; text-align: center;
  background: #4ec9b0; color: #0f1419; border-radius: 5px;
  padding: 0 8px; margin-right: 10px;
}
.sub { color: #6b7a8d; margin-bottom: 26px; font-size: 13px; }
.grid { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 26px; }
.card {
  background: #161c24; border: 1px solid #232c38; border-radius: 6px;
  padding: 11px 17px; min-width: 150px;
}
.card .k { color: #6b7a8d; font-size: 12px; margin-bottom: 4px; }
.card .v { font-size: 16px; color: #e8eef5; word-break: break-all; }
.card .v.mono { font-size: 14px; }
h2 { font-size: 14px; color: #8b9bb0; margin: 0 0 10px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin-bottom: 26px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid #1e2733; }
th { color: #6b7a8d; font-weight: 600; font-size: 12px; }
tr:last-child td { border-bottom: none; }
.stripped { color: #4ec9b0; }
.own      { color: #6b9bd1; }
.leaked   { color: #e06c75; font-weight: 600; }
.dim { color: #6b7a8d; }
.note {
  background: #161c24; border-left: 3px solid #4ec9b0; border-radius: 0 6px 6px 0;
  padding: 12px 18px; margin-bottom: 26px; color: #8b9bb0; font-size: 13px;
}
footer { color: #4a5666; font-size: 12px; border-top: 1px solid #1e2733; padding-top: 14px; }
pre.cmd {
  background: #161c24; border: 1px solid #232c38; border-radius: 6px;
  padding: 12px 16px; margin: 0 0 26px; color: #9fd3c0;
  white-space: pre-wrap; word-break: break-all; font-size: 12.5px;
}
a { color: #4ec9b0; }
"""


class MockBackend(BaseHTTPRequestHandler):
    """假装成一个业务后端：把自己收到的一切原样回显。

    同一个请求有两种呈现方式，靠 `Accept` 协商：

    * 浏览器（`Accept: text/html`）看到一张排版好的页面，字段带标注、
      逐跳首部逐项标出「已剥离」还是「透传」；
    * 脚本（`curl`、`http.client`，`Accept` 是 `*/*` 或没有）拿到**原来那份
      JSON**，键名一个没变。

    这个区分是必要的：JSON 才是可被程序断言的形态，演示用的页面不该把
    机器可读的那份吃掉。想看 JSON 也可以在浏览器里加 `?format=json`。
    """

    protocol_version = "HTTP/1.1"
    sys_version = ""            # 别把 Python 版本号暴露出去
    server_version = "MockBackend/1.0"
    name = "?"

    def log_message(self, *args) -> None:
        pass  # 让代理的访问日志看得清楚

    def _wants_html(self) -> bool:
        if "format=json" in (self.path or ""):
            return False
        return "text/html" in (self.headers.get("Accept") or "")

    def _snapshot(self) -> dict:
        """收集这次请求的全部可观测事实。

        HTML 和 JSON 两种呈现都从这一个 dict 渲染，不会出现"页面显示的
        和 JSON 里的不一致"这种事。
        """
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        return {
            "我是后端": self.name,
            "方法": self.command,
            "路径": self.path,
            "请求体": body.decode("utf-8", "replace"),
            "真实客户端IP": self.headers.get("X-Forwarded-For"),
            "原始Host": self.headers.get("X-Forwarded-Host"),
            "后端实际看到的Host": self.headers.get("Host"),
            "客户端协议": self.headers.get("X-Forwarded-Proto"),
            "逐跳首部已剥离": {
                name: self.headers.get(name) for name in HOP_BY_HOP_PROBE
            },
        }

    def _reply(self) -> None:
        snapshot = self._snapshot()
        if self._wants_html():
            payload = _render_page(snapshot, self.server_version).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            payload = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Backend-Name", self.name)
        # 同一个 URL 会因 Accept 不同返回不同形态，缓存必须按它区分
        self.send_header("Vary", "Accept")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reply
    do_HEAD = _reply
    do_TRACE = _reply


def _card(label: str, value, tone: str = "") -> str:
    cls = f"v {tone}".strip()
    shown = "（未提供）" if value in (None, "") else str(value)
    return (
        f'<div class="card"><div class="k">{html.escape(label)}</div>'
        f'<div class="{cls} mono">{html.escape(shown)}</div></div>'
    )


def _connection_verdict(value):
    """Connection 单独判：返回 (是否泄漏, 泄漏的词)。

    局限说在明处：客户端自己发 ``Connection: close``（urllib 就是这么干的）
    时，它和代理生成的值长得一模一样，这里分不出来。要把这件事钉死只能靠
    用例——``tests/test_proxy.py::TestHeaderHandling`` 里让客户端发
    ``Connection: keep-alive``，再断言后端收到的是 ``close``；页面页脚的
    curl 命令也能手动复现。
    """
    if value is None:
        return False, []
    tokens = [t.strip().lower() for t in str(value).split(",") if t.strip()]
    stray = [t for t in tokens if t not in _PROXY_OWN_CONNECTION_TOKENS]
    return bool(stray), stray


def _audit_hop_by_hop(snapshot: dict):
    """逐跳首部审计，返回 (表格行, 泄漏说明)。

    表格和首屏那句话都从这一个函数出，不会出现「表格画绿的、结论说红的」。

    行是 ``(首部名, 后端收到的值, 色调, 结论)``，色调取 ``stripped`` /
    ``own`` / ``leaked`` / ``dim`` 四选一。
    """
    seen = snapshot["逐跳首部已剥离"]
    rows, leaks = [], []

    for name in HOP_BY_HOP_PROBE:
        value = seen.get(name)

        if name == "Connection":
            bad, stray = _connection_verdict(value)
            if value is None:
                # 本代理总会声明，所以这属于「实现变了」，不是泄漏
                rows.append((name, "（未声明）", "dim", "代理没为这段连接声明 Connection"))
            elif bad:
                leaks.append(f"Connection: {'、'.join(stray)}")
                rows.append((name, value, "leaked", "透传了！客户端发的东西活到了这里"))
            else:
                rows.append((name, value, "own", "代理为自己那段连接生成的（正确）"))
            continue

        if value is None:
            rows.append((name, "—", "stripped", "已剥离"))
        else:
            leaks.append(f"{name}: {value}")
            rows.append((name, value, "leaked", "透传了！"))

    return rows, leaks


def _hop_by_hop_rows(rows) -> str:
    """把审计结果渲染成表格。"""
    cells = []
    for name, shown, tone, verdict in rows:
        if tone == "stripped":
            shown_html = '<span class="dim">—</span>'
        elif tone == "dim":
            shown_html = f'<span class="dim">{html.escape(str(shown))}</span>'
        else:
            shown_html = html.escape(str(shown))
        cells.append(
            f"<tr><td>{html.escape(name)}</td><td>{shown_html}</td>"
            f'<td><span class="{tone}">{html.escape(verdict)}</span></td></tr>'
        )
    return (
        "<table><thead><tr><th>首部</th><th>后端实际收到</th><th>结论</th></tr></thead>"
        "<tbody>" + "".join(cells) + "</tbody></table>"
    )


def _render_page(snapshot: dict, backend_version: str) -> str:
    """把快照渲染成给浏览器看的页面。"""
    rows, leaked = _audit_hop_by_hop(snapshot)
    ok = not leaked

    method = html.escape(str(snapshot["方法"]))
    path = html.escape(str(snapshot["路径"]))
    same_host = snapshot["原始Host"] == snapshot["后端实际看到的Host"]
    # 代理保留了客户端原始 Host，所以这个值就是代理自己的地址，正好能拼出
    # 页脚那条「自己验一遍」的命令
    proxy_addr = snapshot["后端实际看到的Host"] or "127.0.0.1:8080"

    cards = "".join([
        _card("方法", method),
        _card("路径", path),
        _card("真实客户端 IP", snapshot["真实客户端IP"]),
        _card("客户端协议", snapshot["客户端协议"]),
    ])

    body_block = ""
    if snapshot["请求体"]:
        body_block = (
            "<h2>请求体</h2><table><tbody><tr><td>"
            f"<pre style='margin:0;white-space:pre-wrap'>{html.escape(snapshot['请求体'])}</pre>"
            "</td></tr></tbody></table>"
        )

    note = (
        "该剥的逐跳首部全被代理剥掉了，<b>这是对的</b>——它们只在「一段」连接上"
        "有意义，转发到下一段就必须去掉。"
        if ok else
        f"<b>有逐跳首部漏过来了：{html.escape('、'.join(leaked))}</b>——这是代理的 bug。"
    )

    host_note = (
        "代理有意保留客户端原始 Host，方便后端做虚拟主机路由，"
        "原值另由 X-Forwarded-Host 携带。这次两者相同。"
        if same_host else
        "后端看到的 Host 与客户端原始 Host 不同，原值由 X-Forwarded-Host 带过来了。"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>后端 {html.escape(str(snapshot["我是后端"]))} · 收到了什么</title>
<style>{_STYLE}</style>
</head>
<body>

<h1><span class="badge">{html.escape(str(snapshot["我是后端"]))}</span>后端收到了什么</h1>
<div class="sub">
  这一页由<b>模拟后端</b>渲染，不是代理渲染的。刷新几次会看到右上角的字母
  在 A / B / C 之间轮换 —— 那说明请求确实被分摊到了不同后端。
</div>

<div class="grid">{cards}</div>

<div class="note">{note}</div>

<h2>逐跳首部审计（RFC 7230 §6.1）</h2>
<div class="note">
  代理要剥掉的是<b>客户端发来的</b>逐跳首部。<br>
  但 <code>Connection</code> 不能看「有没有值」：逐跳首部是每段连接各管各的，
  代理到后端是<b>新的一段</b>，所以代理<b>必须</b>替这一段自己生成一个。<br>
  <span class="own">蓝色</span>那行是代理自己写的，正确；
  <span class="leaked">红色</span>才是漏。
</div>
{_hop_by_hop_rows(rows)}
<div class="note">
  上面这个判定有个盲区：客户端自己发 <code>Connection: close</code> 时
  （<code>urllib</code> 默认就这么发），它和代理生成的值长得一模一样，从后端看分不出来。
  钉死这一条要靠用例 —— <code>tests/test_proxy.py::TestHeaderHandling</code> 让客户端发
  <code>Connection: keep-alive</code>，再断言后端收到的仍是 <code>close</code>。
</div>

<h2>Host 与转发首部</h2>
<table><tbody>
<tr><th>后端实际看到的 Host</th><td>{html.escape(str(snapshot["后端实际看到的Host"]))}</td></tr>
<tr><th>X-Forwarded-Host</th><td>{html.escape(str(snapshot["原始Host"]))}</td></tr>
<tr><th>X-Forwarded-For</th><td>{html.escape(str(snapshot["真实客户端IP"]))}</td></tr>
<tr><th>X-Forwarded-Proto</th><td>{html.escape(str(snapshot["客户端协议"]))}</td></tr>
</tbody></table>
<div class="note">{host_note}<br>
X-Forwarded-For 是<b>追加</b>而不是覆盖：链路前面还有别的代理时，覆盖会把原始客户端丢掉。</div>

{body_block}

<h2>自己验一遍</h2>
<div class="note">
  复制到终端跑（<b>PowerShell 里要写 <code>curl.exe</code></b>，<code>curl</code> 是
  <code>Invoke-WebRequest</code> 的别名）。这条命令会带着一堆逐跳首部去找代理，
  然后打印后端真正收到了什么：
</div>
<pre class="cmd">curl -s -H "Connection: keep-alive" -H "Keep-Alive: timeout=5" -H "Upgrade: websocket" -H "TE: trailers" "http://{html.escape(str(proxy_addr))}/?format=json"</pre>

<footer>
  想看机器可读的原始数据：<a href="?format=json">?format=json</a> ·
  管理界面：<a href="/__admin">/__admin</a>（仅本机）<br>
  后端 {html.escape(str(snapshot["我是后端"]))} · {html.escape(backend_version)} ·
  这一页的形态由 <code>Accept</code> 协商决定（<code>curl</code> 拿到的仍然是 JSON）
</footer>

</body>
</html>
"""


def free_port() -> int:
    """占一个空闲端口再放掉，得到一个确定没人监听的端口。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def make_backend_class(name: str) -> type:
    """按名字造一个 Handler 子类（后端名要出现在响应里，只能按类区分）。"""
    return type("MockBackend" + name, (MockBackend,), {"name": name})


def start_backends(count: int, host: str = "127.0.0.1"):
    """启动 count 个模拟后端，返回 (服务器列表, 端口列表)。"""
    servers, ports = [], []
    for index in range(count):
        name = chr(ord("A") + index) if index < 26 else f"B{index}"
        server = ThreadingHTTPServer((host, 0), make_backend_class(name))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        ports.append(server.server_address[1])
    return servers, ports


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单进程模拟后端")
    parser.add_argument("-p", "--port", type=int, default=9000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("-n", "--name", default="A", help="后端名，会回显在响应里")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(
        (args.host, args.port), make_backend_class(args.name)
    )
    print(
        f"模拟后端 {args.name} 监听 http://{args.host}:{args.port}/",
        file=sys.stderr, flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
