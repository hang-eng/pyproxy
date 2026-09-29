"""一键演示：起若干个模拟后端 + 一个反向代理，用来直观验证各项功能。

    python demo.py                      # 3 个模拟后端 + 代理（端口 8080）
    python demo.py --dead               # 额外加一个连不上的后端，演示失败重试
    python demo.py --backends 5 -s weighted_round_robin
    python demo.py --port 9000

然后浏览器打开提示里的入口地址反复刷新，能看到 JSON 里的
「我是后端」字段在各后端之间轮换；stdout 上会同步打印访问日志。

模拟后端会把收到的东西原样回显（方法、路径、请求体、X-Forwarded-*、
逐跳首部是否已被剥离），所以只看一个 JSON 就能验证转发是否符合预期。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy.config import Backend, ProxyConfig, VALID_STRATEGIES
from mock_backend import free_port, start_backends
import run as runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="pyproxy 一键演示：起模拟后端 + 代理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-n", "--backends", type=int, default=3,
                        metavar="N", help="模拟后端数量（默认 3）")
    parser.add_argument("-p", "--port", type=int, default=8080,
                        metavar="PORT", help="代理监听端口（默认 8080）")
    parser.add_argument("-s", "--strategy", default="round_robin",
                        choices=list(VALID_STRATEGIES), help="负载均衡策略")
    parser.add_argument("--dead", action="store_true",
                        help="额外加一个故意连不上的后端，用来演示失败重试")
    parser.add_argument("--timeout", type=float, default=5.0,
                        metavar="SEC", help="转发超时（默认 5 秒）")
    parser.add_argument("--no-health-check", action="store_true",
                        help="关闭健康检查")
    parser.add_argument("--rate-limit", type=float, default=None, metavar="N",
                        help="开启限流：每个 IP 每秒放行 N 个请求（默认关闭，可配合 --rate-burst 演示 429）")
    parser.add_argument("--rate-burst", type=int, default=5, metavar="N",
                        help="限流桶容量，即瞬时允许的突发量（默认 5）")
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="访问日志文件路径；默认写到 ./logs/access.log")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    _, ports = start_backends(max(1, args.backends))
    backends = [Backend("127.0.0.1", port, 1) for port in ports]

    dead_port = None
    if args.dead:
        # 插在列表中间，这样大约每 (后端数 + 1) 次请求就有一次落到坏后端上，
        # 能直观看到「先失败、换一台、成功」以及访问日志里的 retry=N
        dead_port = free_port()
        backends.insert(1, Backend("127.0.0.1", dead_port, 1))

    log_file = args.log_file
    if log_file is None:
        log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs", "access.log"
        )

    config = ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=args.port,
        strategy=args.strategy,
        timeout=args.timeout,
        max_retries=2,
        backends=backends,
        log_file=log_file,
        log_stdout=True,
    )
    config.health_check.enabled = not args.no_health_check
    if args.rate_limit is not None:
        config.rate_limit.enabled = True
        config.rate_limit.refill_rate = args.rate_limit
        config.rate_limit.capacity = args.rate_burst

    # 横幅同样走 stderr：stdout 留给访问日志，这样
    # `python demo.py > access.log` 得到的也是一个干净的文件
    out = sys.stderr
    print("=" * 66, file=out)
    print("  模拟后端：", file=out)
    for index, port in enumerate(ports):
        name = chr(ord("A") + index) if index < 26 else f"B{index}"
        print(f"    {name}  http://127.0.0.1:{port}/", file=out)
    if dead_port is not None:
        print(f"    坏  http://127.0.0.1:{dead_port}/   （故意不监听，用来看重试）", file=out)
    print("=" * 66, file=out)
    print(file=out, flush=True)

    runner.setup_logging(config)
    return runner.serve(config)


if __name__ == "__main__":
    sys.exit(main())
