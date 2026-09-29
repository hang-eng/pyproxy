"""Web 管理界面。

访问 ``/__admin`` 可以看到后端健康状态、各策略分发计数、限流命中情况。
纯服务端渲染 + ``meta refresh``，不引任何前端资源——一个代理的管理页
不值得为它加一套构建工具链。

**这一页只对回环地址开放。** 它暴露了后端拓扑（内网地址、权重、健康状态）
和限流参数，属于典型的"给运维看、不该给公网看"的信息。远程客户端访问
``/__admin`` 会拿到 404——不是 403，因为 403 等于告诉对方"这里确实有东西"。
真要远程看，正规做法是在前面加一层鉴权或走独立端口，而不是把这一页放开。
"""

from __future__ import annotations

import html
import ipaddress
from typing import Any, Dict, List, Optional

ADMIN_PATH = "/__admin"

_STYLE = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 32px; background: #0f1419; color: #d8dee9;
  font: 14px/1.6 "Consolas", "Cascadia Mono", "Microsoft YaHei", monospace;
}
h1 { font-size: 19px; margin: 0 0 4px; color: #e8eef5; font-weight: 600; }
h1 .dot { color: #4ec9b0; }
.sub { color: #6b7a8d; margin-bottom: 24px; font-size: 13px; }
.grid { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 26px; }
.card {
  background: #161c24; border: 1px solid #232c38; border-radius: 6px;
  padding: 12px 18px; min-width: 132px;
}
.card .k { color: #6b7a8d; font-size: 12px; margin-bottom: 4px; }
.card .v { font-size: 20px; color: #e8eef5; }
.card .v.good { color: #4ec9b0; }
.card .v.bad  { color: #e06c75; }
h2 { font-size: 14px; color: #8b9bb0; margin: 0 0 10px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin-bottom: 26px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid #1e2733; }
th { color: #6b7a8d; font-weight: 600; font-size: 12px; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; }
.up   { color: #4ec9b0; }
.down { color: #e06c75; }
.dim  { color: #6b7a8d; }
footer { color: #4a5666; font-size: 12px; border-top: 1px solid #1e2733; padding-top: 14px; }
"""


def is_admin_path(path: str) -> bool:
    """判断这个请求目标是不是管理页（忽略查询串）。"""
    target = (path or "").split("?", 1)[0]
    return target.rstrip("/") == ADMIN_PATH or target == ADMIN_PATH + "/"


def is_local_client(client_address: Any) -> bool:
    """客户端是不是来自本机回环地址。"""
    if not client_address:
        return False
    host = client_address[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # IPv6 形式的 IPv4（::ffff:127.0.0.1）也要认出来
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def _card(label: str, value: Any, tone: str = "") -> str:
    cls = f"v {tone}".strip()
    return (f'<div class="card"><div class="k">{html.escape(label)}</div>'
            f'<div class="{cls}">{html.escape(str(value))}</div></div>')


def _backend_rows(balancer_stats: Dict, health_snapshot: Optional[List[Dict]]) -> str:
    health_by_address = {}
    for row in health_snapshot or []:
        health_by_address[row.get("address")] = row

    rows = []
    for backend in balancer_stats.get("backends", []):
        address = backend.get("address", "?")
        healthy = backend.get("healthy", True)
        detail = health_by_address.get(address, {})
        status = ('<span class="up">健康</span>' if healthy
                  else '<span class="down">已剔除</span>')
        rows.append(
            "<tr>"
            f"<td>{html.escape(address)}</td>"
            f'<td class="num">{backend.get("weight", "-")}</td>'
            f'<td class="num">{backend.get("total_requests", 0)}</td>'
            f"<td>{status}</td>"
            f'<td class="num">{detail.get("consecutive_failures", 0)}</td>'
            f'<td class="num">{detail.get("consecutive_successes", 0)}</td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="dim">没有配置后端</td></tr>')

    return (
        "<table><thead><tr>"
        "<th>后端</th><th>权重</th><th>已转发</th><th>状态</th>"
        "<th>连续失败</th><th>连续成功</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render(ctx: Any, uptime: float) -> bytes:
    """把当前运行状态渲染成 HTML 页面。"""
    config = ctx.config
    stats = ctx.stats()

    balancer_stats = ctx.balancer.stats()
    health_snapshot = None
    if ctx.health_checker is not None:
        try:
            health_snapshot = ctx.health_checker.snapshot()
        except Exception:
            health_snapshot = None

    total = stats["total_requests"]
    failures = stats["status_5xx"] + stats["status_4xx"]
    ok_rate = f"{(total - failures) / total * 100:.1f}%" if total else "-"
    healthy_count = sum(1 for b in balancer_stats.get("backends", []) if b.get("healthy"))
    backend_count = len(balancer_stats.get("backends", []))

    cards = "".join([
        _card("运行时长", _format_duration(uptime)),
        _card("总请求", total),
        _card("成功率", ok_rate, "good" if total and not failures else ""),
        _card("4xx / 5xx", f'{stats["status_4xx"]} / {stats["status_5xx"]}',
              "bad" if stats["status_5xx"] else ""),
        _card("重试次数", stats["total_retries"]),
        _card("已转发字节", f'{stats["total_bytes"] / 1024:.1f} KiB'),
        _card("健康后端", f"{healthy_count} / {backend_count}",
              "good" if healthy_count == backend_count and backend_count else "bad"),
    ])

    if ctx.rate_limiter is not None:
        limit = ctx.rate_limiter.stats()
        rate_cards = "".join([
            _card("限流", "已启用" if limit["enabled"] else "已关闭",
                  "good" if limit["enabled"] else ""),
            _card("放行", limit["allowed"]),
            _card("拒绝", limit["rejected"], "bad" if limit["rejected"] else ""),
            _card("追踪客户端", f'{limit["tracked_clients"]} / {limit["max_buckets"]}'),
        ])
    else:
        rate_cards = _card("限流", "未装配")

    hc = config.health_check
    if hc.enabled:
        health_line = (f"每 {hc.interval}s 探测 {html.escape(hc.path)}，"
                       f"连续失败 {hc.unhealthy_threshold} 次剔除 / "
                       f"连续成功 {hc.healthy_threshold} 次恢复")
    else:
        health_line = "已关闭"

    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>反向代理 · 运行状态</title>
<style>{_STYLE}</style>
</head>
<body>
<h1><span class="dot">●</span> 反向代理 · 运行状态</h1>
<div class="sub">
  监听 {html.escape(config.listen_host)}:{config.listen_port} ·
  策略 {html.escape(balancer_stats.get("strategy", config.strategy))} ·
  超时 {config.timeout}s · 最大重试 {config.max_retries} 次 ·
  每 2 秒自动刷新
</div>

<div class="grid">{cards}</div>

<h2>后端</h2>
{_backend_rows(balancer_stats, health_snapshot)}

<h2>限流</h2>
<div class="grid">{rate_cards}</div>

<h2>配置摘要</h2>
<table><tbody>
<tr><th>健康检查</th><td>{health_line}</td></tr>
<tr><th>访问日志</th><td>{html.escape(str(config.log_file) if config.log_file else "（不写文件）")}</td></tr>
<tr><th>转发超时</th><td>{config.timeout}s</td></tr>
</tbody></table>

<footer>
  本页仅对回环地址开放。远程访问返回 404，不暴露它的存在。<br>
  数据是渲染时的快照，不落库、不保留历史。
</footer>
</body>
</html>
"""
    return body.encode("utf-8")
