"""命令行入口的参数定义。

所有参与配置覆盖的参数默认值都是 None，用于区分
「用户显式传了值」和「用户没传」，后者不应覆盖配置文件。
"""

from __future__ import annotations

import argparse
import sys

from .config import VALID_STRATEGIES

EPILOG = """\
示例：
  # 用配置文件启动
  python run.py -c config.json

  # 不用配置文件，全部走命令行
  python run.py -b 127.0.0.1:8001,127.0.0.1:8002,127.0.0.1:8003:2 -s weighted_round_robin

  # 命令行覆盖配置文件里的端口与策略
  python run.py -c config.json -p 9000 -s random

  # 只看最终生效的配置，不启动服务
  python run.py -c config.json --print-config

启动后可用 curl 验证：
  curl -i http://127.0.0.1:8080/
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="pyproxy —— 纯 Python 标准库实现的 HTTP 反向代理与负载均衡器",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    basic = parser.add_argument_group("基本参数")
    basic.add_argument(
        "-c", "--config",
        metavar="PATH",
        help="JSON 配置文件路径；未提供时全部使用默认值与命令行参数",
    )
    basic.add_argument(
        "--listen-host",
        metavar="HOST",
        help="监听地址（默认 0.0.0.0）",
    )
    basic.add_argument(
        "-p", "--listen-port",
        type=int,
        metavar="PORT",
        help="监听端口（默认 8080）",
    )
    basic.add_argument(
        "-b", "--backends",
        metavar="LIST",
        help="后端列表，逗号分隔；支持 host:port 和 host:port:weight 两种写法",
    )
    basic.add_argument(
        "-s", "--strategy",
        choices=list(VALID_STRATEGIES),
        help="负载均衡策略（默认 round_robin）",
    )

    forward = parser.add_argument_group("转发参数")
    forward.add_argument(
        "-t", "--timeout",
        type=float,
        metavar="SEC",
        help="转发超时秒数（默认 5）",
    )
    forward.add_argument(
        "-r", "--max-retries",
        type=int,
        metavar="N",
        help="失败后最大重试次数，重试会换一个后端（默认 2）",
    )

    health = parser.add_argument_group("健康检查")
    health.add_argument(
        "--health-interval",
        type=float,
        metavar="SEC",
        help="健康检查周期（默认 3）",
    )
    health.add_argument(
        "--health-timeout",
        type=float,
        metavar="SEC",
        help="单次健康检查超时（默认 1）",
    )
    health.add_argument(
        "--health-path",
        metavar="PATH",
        help="健康检查路径（默认 /health）",
    )
    health.add_argument(
        "--no-health-check",
        action="store_true",
        default=None,
        help="关闭健康检查，所有后端始终视为可用",
    )

    ratelimit = parser.add_argument_group("限流")
    ratelimit.add_argument(
        "--rate-limit",
        type=float,
        metavar="RPS",
        help="开启限流并指定每 IP 每秒补充的令牌数",
    )
    ratelimit.add_argument(
        "--rate-burst",
        type=int,
        metavar="N",
        help="令牌桶容量（默认 20）",
    )

    log = parser.add_argument_group("日志")
    log.add_argument(
        "--log-file",
        metavar="PATH",
        help="访问日志文件路径；目录不存在会自动创建",
    )
    log.add_argument(
        "--quiet",
        action="store_true",
        default=None,
        help="不把访问日志输出到 stdout（默认同时输出到 stdout）",
    )
    log.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=None,
        help="输出调试日志",
    )

    misc = parser.add_argument_group("其他")
    misc.add_argument(
        "--print-config",
        action="store_true",
        help="打印最终生效的配置后退出，不启动服务",
    )

    return parser


def parse_args(argv=None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def fatal(message: str) -> None:
    """打印错误到 stderr 并退出。"""
    print(f"[配置错误] {message}", file=sys.stderr)
    raise SystemExit(2)
