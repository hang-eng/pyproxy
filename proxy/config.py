"""配置定义与加载。

生效优先级：命令行参数 > 配置文件 > 内置默认值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 支持的负载均衡策略
VALID_STRATEGIES = (
    "round_robin",
    "random",
    "weighted_round_robin",
    "weighted_random",
)


class ConfigError(Exception):
    """配置缺失或非法时抛出，由入口统一捕获并友好提示。"""


@dataclass
class Backend:
    """一个后端实例。

    前三个字段是静态配置，来自配置文件或命令行。
    其余字段是运行时状态：由健康检查线程写入、由负载均衡器读写，
    因此访问时必须在同一个锁下进行（见 health / balancer）。
    """

    host: str
    port: int
    weight: int = 1

    healthy: bool = True
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    current_weight: int = 0
    effective_weight: int = 1
    total_requests: int = 0

    def __post_init__(self) -> None:
        # effective_weight 是运行时可调的权重，初始值必须等于配置权重。
        # 放在这里而不是让 parse_backend 事后补写，是为了保证任何方式
        # 构造出的 Backend 都自洽（否则直接构造时权重会静默失效）。
        self.effective_weight = self.weight

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.address


@dataclass
class HealthCheckConfig:
    """健康检查参数。

    consecutive_* 阈值用于防止网络抖动导致状态反复横跳（flapping）：
    必须连续失败 N 次才判定为不健康，连续成功 M 次才判定为恢复。
    """

    enabled: bool = True
    interval: float = 3.0
    timeout: float = 1.0
    path: str = "/health"
    unhealthy_threshold: int = 3
    healthy_threshold: int = 2


@dataclass
class RateLimitConfig:
    """令牌桶限流参数（每个客户端 IP 一个桶）。"""

    enabled: bool = False
    capacity: int = 20
    refill_rate: float = 10.0


@dataclass
class ProxyConfig:
    """代理的完整生效配置。"""

    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    strategy: str = "round_robin"
    timeout: float = 5.0
    max_retries: int = 2
    backends: List[Backend] = field(default_factory=list)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    log_file: Optional[str] = None
    log_stdout: bool = True
    verbose: bool = False


# --------------------------------------------------------------------------
# 后端解析
# --------------------------------------------------------------------------

def parse_backend(raw: Any) -> Backend:
    """把一条后端配置解析成 Backend。

    支持三种写法：
        "127.0.0.1:8001"                -> 权重 1
        "127.0.0.1:8001:3"              -> 权重 3
        {"host": "...", "port": 1, "weight": 2}
    """
    if isinstance(raw, dict):
        host = raw.get("host")
        port = raw.get("port")
        if host in (None, "") or port is None:
            raise ConfigError(f"后端配置缺少 host 或 port：{raw!r}")
        try:
            backend = Backend(str(host).strip(), int(port), int(raw.get("weight", 1)))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"后端配置字段类型错误：{raw!r} ({exc})") from exc

    elif isinstance(raw, str):
        parts = [p.strip() for p in raw.split(":")]
        try:
            if len(parts) == 2:
                backend = Backend(parts[0], int(parts[1]), 1)
            elif len(parts) == 3:
                backend = Backend(parts[0], int(parts[1]), int(parts[2]))
            else:
                raise ValueError
        except ValueError as exc:
            raise ConfigError(
                f"无法解析后端地址 {raw!r}，应为 host:port 或 host:port:weight"
            ) from exc

    else:
        raise ConfigError(f"不支持的后端配置类型：{type(raw).__name__}")

    if not backend.host:
        raise ConfigError(f"后端主机名为空：{raw!r}")
    if not 0 < backend.port < 65536:
        raise ConfigError(f"后端端口越界：{backend.port}（应在 1~65535）")
    if backend.weight < 1:
        raise ConfigError(f"后端权重必须 >= 1：{backend.address} weight={backend.weight}")

    return backend


def parse_backends(raw_list: Any) -> List[Backend]:
    """解析后端列表，接受逗号分隔字符串或列表。"""
    if isinstance(raw_list, str):
        items: List[Any] = [x for x in raw_list.split(",") if x.strip()]
    elif isinstance(raw_list, list):
        items = raw_list
    else:
        raise ConfigError("backends 必须是数组，或形如 host:port,host:port 的字符串")

    if not items:
        raise ConfigError("backends 为空")

    backends = [parse_backend(item) for item in items]
    seen = set()
    for backend in backends:
        if backend.address in seen:
            raise ConfigError(f"后端地址重复：{backend.address}")
        seen.add(backend.address)
    return backends


# --------------------------------------------------------------------------
# 配置文件加载
# --------------------------------------------------------------------------

def _cast(value: Any, caster, field_name: str):
    try:
        return caster(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {field_name} 类型错误：{value!r} ({exc})") from exc


def from_dict(data: Dict[str, Any]) -> ProxyConfig:
    """把配置文件字典转换成 ProxyConfig（只覆盖出现过的键）。"""
    cfg = ProxyConfig()

    if "listen_host" in data:
        cfg.listen_host = str(data["listen_host"])
    if "listen_port" in data:
        cfg.listen_port = _cast(data["listen_port"], int, "listen_port")
    if "strategy" in data:
        cfg.strategy = str(data["strategy"])
    if "timeout" in data:
        cfg.timeout = _cast(data["timeout"], float, "timeout")
    if "max_retries" in data:
        cfg.max_retries = _cast(data["max_retries"], int, "max_retries")
    if "backends" in data:
        cfg.backends = parse_backends(data["backends"])
    if "log_file" in data:
        cfg.log_file = data["log_file"]
    if "log_stdout" in data:
        cfg.log_stdout = bool(data["log_stdout"])
    if "verbose" in data:
        cfg.verbose = bool(data["verbose"])

    hc = data.get("health_check")
    if hc is not None:
        if not isinstance(hc, dict):
            raise ConfigError("health_check 必须是对象")
        if "enabled" in hc:
            cfg.health_check.enabled = bool(hc["enabled"])
        if "interval" in hc:
            cfg.health_check.interval = _cast(hc["interval"], float, "health_check.interval")
        if "timeout" in hc:
            cfg.health_check.timeout = _cast(hc["timeout"], float, "health_check.timeout")
        if "path" in hc:
            cfg.health_check.path = str(hc["path"])
        if "unhealthy_threshold" in hc:
            cfg.health_check.unhealthy_threshold = _cast(
                hc["unhealthy_threshold"], int, "health_check.unhealthy_threshold"
            )
        if "healthy_threshold" in hc:
            cfg.health_check.healthy_threshold = _cast(
                hc["healthy_threshold"], int, "health_check.healthy_threshold"
            )

    rl = data.get("rate_limit")
    if rl is not None:
        if not isinstance(rl, dict):
            raise ConfigError("rate_limit 必须是对象")
        if "enabled" in rl:
            cfg.rate_limit.enabled = bool(rl["enabled"])
        if "capacity" in rl:
            cfg.rate_limit.capacity = _cast(rl["capacity"], int, "rate_limit.capacity")
        if "refill_rate" in rl:
            cfg.rate_limit.refill_rate = _cast(rl["refill_rate"], float, "rate_limit.refill_rate")

    return cfg


def load_file(path: str) -> ProxyConfig:
    """读取 JSON 配置文件。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}（{exc}）") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是对象：{path}")
    return from_dict(data)


# --------------------------------------------------------------------------
# 命令行覆盖
# --------------------------------------------------------------------------

def apply_cli_overrides(cfg: ProxyConfig, args: Any) -> ProxyConfig:
    """把命令行显式提供的参数覆盖到配置上。

    argparse 里所有参与覆盖的参数默认值都是 None，
    因此「None」表示用户没传，不能覆盖配置文件的值。
    """
    def given(name: str) -> bool:
        value = getattr(args, name, None)
        return value is not None and value != ""

    if given("listen_host"):
        cfg.listen_host = str(args.listen_host)
    if given("listen_port"):
        cfg.listen_port = int(args.listen_port)
    if given("strategy"):
        cfg.strategy = str(args.strategy)
    if given("timeout"):
        cfg.timeout = float(args.timeout)
    if given("max_retries"):
        cfg.max_retries = int(args.max_retries)
    if given("backends"):
        cfg.backends = parse_backends(args.backends)
    if given("log_file"):
        cfg.log_file = str(args.log_file)
    if getattr(args, "quiet", None):
        cfg.log_stdout = False
    if getattr(args, "verbose", None):
        cfg.verbose = True

    if given("health_interval"):
        cfg.health_check.interval = float(args.health_interval)
    if given("health_timeout"):
        cfg.health_check.timeout = float(args.health_timeout)
    if given("health_path"):
        cfg.health_check.path = str(args.health_path)
    if getattr(args, "no_health_check", None):
        cfg.health_check.enabled = False

    if given("rate_limit"):
        cfg.rate_limit.enabled = True
        cfg.rate_limit.refill_rate = float(args.rate_limit)
    if given("rate_burst"):
        cfg.rate_limit.capacity = int(args.rate_burst)

    return cfg


# --------------------------------------------------------------------------
# 校验与入口
# --------------------------------------------------------------------------

def validate(cfg: ProxyConfig) -> ProxyConfig:
    """校验配置自洽性，不合法直接抛 ConfigError。"""
    if cfg.strategy not in VALID_STRATEGIES:
        raise ConfigError(
            f"未知的负载均衡策略 {cfg.strategy!r}，可选：{', '.join(VALID_STRATEGIES)}"
        )
    if not 0 < cfg.listen_port < 65536:
        raise ConfigError(f"监听端口越界：{cfg.listen_port}（应在 1~65535）")
    if cfg.timeout <= 0:
        raise ConfigError("timeout 必须大于 0")
    if cfg.max_retries < 0:
        raise ConfigError("max_retries 不能为负数")

    hc = cfg.health_check
    if hc.interval <= 0:
        raise ConfigError("health_check.interval 必须大于 0")
    if hc.timeout <= 0:
        raise ConfigError("health_check.timeout 必须大于 0")
    if hc.timeout > hc.interval:
        raise ConfigError(
            f"health_check.timeout（{hc.timeout}s）不应大于 interval（{hc.interval}s）"
        )
    if hc.unhealthy_threshold < 1:
        raise ConfigError("health_check.unhealthy_threshold 必须 >= 1")
    if hc.healthy_threshold < 1:
        raise ConfigError("health_check.healthy_threshold 必须 >= 1")
    if hc.enabled and not hc.path.startswith("/"):
        raise ConfigError(f"health_check.path 必须以 / 开头：{hc.path}")

    rl = cfg.rate_limit
    if rl.enabled:
        if rl.capacity < 1:
            raise ConfigError("rate_limit.capacity 必须 >= 1")
        if rl.refill_rate <= 0:
            raise ConfigError("rate_limit.refill_rate 必须大于 0")

    if not cfg.backends:
        raise ConfigError("未配置任何后端，请用 --backends 或配置文件指定")

    return cfg


def build_config(args: Any) -> ProxyConfig:
    """从命令行参数构建最终生效配置。"""
    config_path = getattr(args, "config", None)
    cfg = load_file(config_path) if config_path else ProxyConfig()
    return validate(apply_cli_overrides(cfg, args))


def config_to_dict(cfg: ProxyConfig) -> Dict[str, Any]:
    """把生效配置还原成可 JSON 序列化的字典（不含运行时状态）。"""
    return {
        "listen_host": cfg.listen_host,
        "listen_port": cfg.listen_port,
        "strategy": cfg.strategy,
        "timeout": cfg.timeout,
        "max_retries": cfg.max_retries,
        "log_file": cfg.log_file,
        "log_stdout": cfg.log_stdout,
        "health_check": {
            "enabled": cfg.health_check.enabled,
            "interval": cfg.health_check.interval,
            "timeout": cfg.health_check.timeout,
            "path": cfg.health_check.path,
            "unhealthy_threshold": cfg.health_check.unhealthy_threshold,
            "healthy_threshold": cfg.health_check.healthy_threshold,
        },
        "rate_limit": {
            "enabled": cfg.rate_limit.enabled,
            "capacity": cfg.rate_limit.capacity,
            "refill_rate": cfg.rate_limit.refill_rate,
        },
        "backends": [
            {"host": b.host, "port": b.port, "weight": b.weight}
            for b in cfg.backends
        ],
    }
