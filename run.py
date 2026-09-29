"""程序入口：装配各个组件并启动代理。

装配顺序（也是启动的依赖顺序）：

    解析配置 -> 配置日志 -> 建负载均衡器 -> 建访问日志 -> 建服务器
             -> 启动健康检查线程 -> 进入请求循环 -> 收到中断后逆序收尾

日志分两个通道，刻意分开：

* **访问日志**（access_log）走 stdout 和/或文件，一行一条结构化记录。
  需要能被 grep/awk 直接处理，所以格式固定。
* **诊断日志**（logging）走 stderr，格式是给人看的。

分开的好处是 `python run.py ... > access.log` 能把访问日志干净地导出去，
而启动横幅、告警、异常堆栈仍然显示在终端上。这和 nginx 把 access_log
与 error_log 分开是一个道理。

退出：Ctrl+C 触发优雅退出——停健康检查线程、关访问日志、关监听套接字。
连按两次 Ctrl+C 会强制退出（万一有请求线程卡住不肯收尾）。
Windows 下 Ctrl+Break（SIGBREAK）走同一条路径。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from typing import Optional

# 让中文在 Windows 管道/重定向里也不乱码。必须在任何输出之前执行。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from proxy.access_log import AccessLogger
from proxy.balancer import create_balancer
from proxy.cli import build_parser
from proxy.config import ConfigError, ProxyConfig, build_config, config_to_dict
from proxy.health import HealthChecker
from proxy.ratelimit import RateLimiter
from proxy.server import ProxyServer, create_server

LOG = logging.getLogger("proxy.run")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

BANNER_WIDTH = 66


# --------------------------------------------------------------------------
# 日志装配
# --------------------------------------------------------------------------

def setup_logging(config: ProxyConfig) -> None:
    """配置诊断日志：一律走 stderr，级别由 --verbose 决定。"""
    level = logging.DEBUG if config.verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))

    root = logging.getLogger("proxy")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # 不要往上层传：否则根 logger 没配 handler 时会打一句
    # "No handlers could be found"，配了又可能重复输出一遍
    root.propagate = False


def build_access_logger(config: ProxyConfig) -> Optional[AccessLogger]:
    """按配置建访问日志；打不开日志文件不该拖住代理启动。"""
    if config.log_file is None and not config.log_stdout:
        LOG.warning("访问日志既没配文件也没开终端输出，请求记录将无处可去")
        return None
    try:
        return AccessLogger(config.log_file, to_stdout=config.log_stdout)
    except OSError as exc:
        print(
            f"[警告] 无法打开访问日志文件 {config.log_file}：{exc}\n"
            f"       已退化为只输出到终端",
            file=sys.stderr,
        )
        return AccessLogger(None, to_stdout=True)


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------

def format_config(config: ProxyConfig) -> str:
    """把生效配置渲染成给人看的表格。"""
    lines = [
        "生效配置",
        "-" * BANNER_WIDTH,
        f"  监听地址      {config.listen_host}:{config.listen_port}",
        f"  负载均衡策略  {config.strategy}",
        f"  转发超时      {config.timeout}s",
        f"  最大重试      {config.max_retries} 次（仅幂等方法，且会换后端）",
        f"  诊断日志      {'DEBUG' if config.verbose else 'INFO'}",
        f"  访问日志      {config.log_file or '（不写文件）'}"
        f"{' + 终端' if config.log_stdout else ''}",
    ]

    hc = config.health_check
    if hc.enabled:
        lines.append(
            f"  健康检查      每 {hc.interval}s 探测 {hc.path}（超时 {hc.timeout}s，"
            f"连续失败 {hc.unhealthy_threshold} 次剔除 / 连续成功 {hc.healthy_threshold} 次恢复）"
        )
    else:
        lines.append("  健康检查      已关闭（所有后端始终视为可用）")

    rl = config.rate_limit
    if rl.enabled:
        lines.append(
            f"  限流          每 IP {rl.refill_rate} 令牌/秒，桶容量 {rl.capacity}"
        )
    else:
        lines.append("  限流          已关闭")

    lines.append(f"  后端          {len(config.backends)} 个")
    for index, backend in enumerate(config.backends):
        lines.append(
            f"    [{index + 1}] {backend.address}  权重 {backend.weight}"
        )
    return "\n".join(lines)


def format_banner(config: ProxyConfig, server: ProxyServer) -> str:
    """启动横幅。写到 stderr，以免污染 stdout 上的访问日志。"""
    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host

    lines = [
        "=" * BANNER_WIDTH,
        "  pyproxy · 轻量 HTTP 反向代理已启动",
        "=" * BANNER_WIDTH,
        format_config(config),
        "-" * BANNER_WIDTH,
        f"  入口地址      http://{host}:{port}/",
        f"  本机访问      http://{display_host}:{port}/",
        f"  管理界面      http://{display_host}:{port}/__admin  （仅限本机）",
    ]
    if config.log_file:
        lines.append(f"  访问日志文件  {os.path.abspath(config.log_file)}")
    lines += [
        "-" * BANNER_WIDTH,
        "  按 Ctrl+C 停止（连按两次强制退出）",
        "=" * BANNER_WIDTH,
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

def install_signal_handlers(server: ProxyServer) -> None:
    """让 Ctrl+C / SIGTERM 走优雅退出，而不是抛 KeyboardInterrupt。

    serve_forever() 必须由**别的线程**调 shutdown()，否则会死锁：
    shutdown() 会一直等 serve_forever 退出，而后者占着当前线程。
    所以信号处理函数里另起一个线程去调。
    """
    state = {"closing": False}

    def handler(signum, _frame):
        if state["closing"]:
            # 第二次中断：可能在等某个卡住的请求线程，直接走
            print("\n再次收到中断，强制退出。", file=sys.stderr)
            sys.stderr.flush()
            os._exit(1)
        state["closing"] = True
        LOG.info("收到信号 %s，正在关闭（再按一次 Ctrl+C 强制退出）……", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    # SIGBREAK 只在 Windows 上有：控制台里按 Ctrl+Break 会发这个信号。
    # 一并接上，免得那种情况下直接崩掉而不是优雅收尾。
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # ValueError: 不在主线程（比如被当模块导入后由测试调用）
            # OSError: 平台不支持
            LOG.debug("无法为 %s 安装信号处理器", sig_name)


def serve(config: ProxyConfig) -> int:
    """装配并运行代理，直到收到停止信号。"""
    access_logger = build_access_logger(config)
    balancer = create_balancer(config.strategy, config.backends)
    health_checker = HealthChecker(config.backends, config.health_check)
    # 即使没开启也建出来：没有后台线程、不占资源，但管理页能如实显示
    # 「已关闭」而不是「未装配」——两者对排查问题意义不同。
    rate_limiter = RateLimiter(config.rate_limit)

    try:
        server = create_server(
            config,
            balancer=balancer,
            health_checker=health_checker,
            access_logger=access_logger,
            rate_limiter=rate_limiter,
        )
    except OSError as exc:
        # 端口被占、地址不可用等。这里是最常见的启动失败原因，
        # 给一句人话而不是抛堆栈。
        print(
            f"[启动失败] 无法监听 {config.listen_host}:{config.listen_port}：{exc}\n"
            f"          端口可能已被占用，可用 -p 换一个端口。",
            file=sys.stderr,
        )
        if access_logger is not None:
            access_logger.close()
        return 1

    health_checker.start()
    print(format_banner(config, server), file=sys.stderr, flush=True)

    install_signal_handlers(server)

    try:
        server.serve_forever()
    finally:
        # 逆序收尾：先停止接受新请求，再停后台线程，最后关日志
        server.server_close()
        health_checker.stop()
        if access_logger is not None:
            access_logger.close()
        LOG.info("代理已停止")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    setup_logging(config)

    if args.print_config:
        print(json.dumps(config_to_dict(config), ensure_ascii=False, indent=2))
        return 0

    return serve(config)


if __name__ == "__main__":
    sys.exit(main())
