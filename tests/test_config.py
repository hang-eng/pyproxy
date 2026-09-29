"""配置层测试：解析、加载、命令行覆盖、校验。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from proxy.config import (
    Backend,
    ConfigError,
    ProxyConfig,
    apply_cli_overrides,
    build_config,
    config_to_dict,
    from_dict,
    load_file,
    parse_backend,
    parse_backends,
    validate,
)


class Args:
    """冒充 argparse 的结果对象。

    apply_cli_overrides 依赖「没传的参数取值是 None」这一约定，
    所以这里默认所有字段都是 None，需要时再逐个赋值。
    """

    def __init__(self, **kwargs):
        self.config = None
        self.listen_host = None
        self.listen_port = None
        self.strategy = None
        self.timeout = None
        self.max_retries = None
        self.backends = None
        self.log_file = None
        self.quiet = False
        self.verbose = False
        self.health_interval = None
        self.health_timeout = None
        self.health_path = None
        self.no_health_check = False
        self.rate_limit = None
        self.rate_burst = None
        self.__dict__.update(kwargs)


class TestParseBackend(unittest.TestCase):

    def test_host_port(self):
        b = parse_backend("127.0.0.1:8001")
        self.assertEqual((b.host, b.port, b.weight), ("127.0.0.1", 8001, 1))

    def test_host_port_weight(self):
        b = parse_backend("10.0.0.7:9000:5")
        self.assertEqual((b.host, b.port, b.weight), ("10.0.0.7", 9000, 5))

    def test_dict_form(self):
        b = parse_backend({"host": "example.com", "port": 80, "weight": 2})
        self.assertEqual((b.host, b.port, b.weight), ("example.com", 80, 2))

    def test_dict_without_weight_defaults_to_one(self):
        self.assertEqual(parse_backend({"host": "a", "port": 1}).weight, 1)

    def test_whitespace_is_trimmed(self):
        b = parse_backend(" 127.0.0.1 : 8080 : 3 ")
        self.assertEqual((b.host, b.port, b.weight), ("127.0.0.1", 8080, 3))

    def test_missing_port_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1")

    def test_too_many_parts_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("a:b:c:d")

    def test_non_numeric_port_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1:abc")

    def test_port_out_of_range_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1:0")
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1:65536")

    def test_zero_weight_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1:80:0")

    def test_negative_weight_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend("127.0.0.1:80:-1")

    def test_empty_host_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend(":80")

    def test_unsupported_type_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend(123)
        with self.assertRaises(ConfigError):
            parse_backend(None)

    def test_dict_missing_host_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backend({"port": 80})


class TestParseBackends(unittest.TestCase):

    def test_comma_separated(self):
        backends = parse_backends("127.0.0.1:1,127.0.0.1:2:3")
        self.assertEqual([b.address for b in backends], ["127.0.0.1:1", "127.0.0.1:2"])
        self.assertEqual(backends[1].weight, 3)

    def test_list_form(self):
        backends = parse_backends([{"host": "a", "port": 1}, "b:2"])
        self.assertEqual([b.address for b in backends], ["a:1", "b:2"])

    def test_trailing_comma_ignored(self):
        self.assertEqual(len(parse_backends("a:1,b:2,")), 2)

    def test_empty_string_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backends("")

    def test_empty_list_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backends([])

    def test_whitespace_only_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backends("  ,  ")

    def test_duplicate_address_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_backends("127.0.0.1:80,127.0.0.1:80")
        self.assertIn("重复", str(ctx.exception))

    def test_wrong_type_rejected(self):
        with self.assertRaises(ConfigError):
            parse_backends(42)


class TestBackendDataclass(unittest.TestCase):

    def test_effective_weight_tracks_configured_weight(self):
        # 直接构造（不经过 parse_backend）时也必须自洽，
        # 否则加权策略会静默按权重 1 分发
        self.assertEqual(Backend("h", 1, 7).effective_weight, 7)

    def test_default_effective_weight(self):
        self.assertEqual(Backend("h", 1).effective_weight, 1)

    def test_start_healthy(self):
        b = Backend("h", 1)
        self.assertTrue(b.healthy)
        self.assertEqual(b.total_requests, 0)
        self.assertEqual(b.consecutive_failures, 0)

    def test_address_and_str(self):
        b = Backend("10.1.2.3", 8080)
        self.assertEqual(b.address, "10.1.2.3:8080")
        self.assertEqual(str(b), "10.1.2.3:8080")


class TestFromDict(unittest.TestCase):

    def test_empty_dict_keeps_defaults(self):
        cfg = from_dict({})
        self.assertEqual(cfg.listen_port, 8080)
        self.assertEqual(cfg.strategy, "round_robin")
        self.assertEqual(cfg.backends, [])
        self.assertTrue(cfg.health_check.enabled)

    def test_partial_override(self):
        cfg = from_dict({"listen_port": 9000, "strategy": "random"})
        self.assertEqual(cfg.listen_port, 9000)
        self.assertEqual(cfg.strategy, "random")
        self.assertEqual(cfg.timeout, 5.0)      # 未出现的键保持默认

    def test_backends_parsed(self):
        cfg = from_dict({"backends": ["a:1", "b:2:4"]})
        self.assertEqual(len(cfg.backends), 2)
        self.assertEqual(cfg.backends[1].weight, 4)

    def test_health_check_subtree(self):
        cfg = from_dict({"health_check": {
            "enabled": False, "interval": 1.5, "timeout": 0.5,
            "path": "/ping", "unhealthy_threshold": 5, "healthy_threshold": 3,
        }})
        hc = cfg.health_check
        self.assertEqual(
            (hc.enabled, hc.interval, hc.timeout, hc.path,
             hc.unhealthy_threshold, hc.healthy_threshold),
            (False, 1.5, 0.5, "/ping", 5, 3),
        )

    def test_rate_limit_subtree(self):
        cfg = from_dict({"rate_limit": {"enabled": True, "capacity": 7, "refill_rate": 2.5}})
        self.assertEqual(
            (cfg.rate_limit.enabled, cfg.rate_limit.capacity, cfg.rate_limit.refill_rate),
            (True, 7, 2.5),
        )

    def test_type_error_reports_field_name(self):
        with self.assertRaises(ConfigError) as ctx:
            from_dict({"listen_port": "not-a-number"})
        self.assertIn("listen_port", str(ctx.exception))

    def test_health_check_must_be_object(self):
        with self.assertRaises(ConfigError):
            from_dict({"health_check": [1, 2]})

    def test_rate_limit_must_be_object(self):
        with self.assertRaises(ConfigError):
            from_dict({"rate_limit": "on"})


class TestLoadFile(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_valid_file(self):
        self.write(json.dumps({"listen_port": 8123, "backends": ["127.0.0.1:1"]}))
        cfg = load_file(self.path)
        self.assertEqual(cfg.listen_port, 8123)
        self.assertEqual(len(cfg.backends), 1)

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_file(self.path + ".nope")
        self.assertIn("不存在", str(ctx.exception))

    def test_invalid_json(self):
        self.write("{ this is not json")
        with self.assertRaises(ConfigError) as ctx:
            load_file(self.path)
        self.assertIn("JSON", str(ctx.exception))

    def test_root_must_be_object(self):
        self.write("[1, 2, 3]")
        with self.assertRaises(ConfigError) as ctx:
            load_file(self.path)
        self.assertIn("根节点", str(ctx.exception))

    def test_chinese_content(self):
        # 配置文件里出现中文（比如日志路径）不该出问题
        self.write(json.dumps({"log_file": "C:/日志/访问.log"}, ensure_ascii=False))
        self.assertEqual(load_file(self.path).log_file, "C:/日志/访问.log")


class TestCliOverrides(unittest.TestCase):

    def base(self) -> ProxyConfig:
        cfg = ProxyConfig()
        cfg.backends = parse_backends("127.0.0.1:1")
        return cfg

    def test_none_does_not_override(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args())
        self.assertEqual(cfg.listen_port, 8080)
        self.assertEqual(cfg.strategy, "round_robin")
        self.assertEqual(cfg.log_file, None)

    def test_explicit_values_override(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(
            listen_host="127.0.0.1", listen_port=9999, strategy="random",
            timeout=1.5, max_retries=0,
        ))
        self.assertEqual(cfg.listen_host, "127.0.0.1")
        self.assertEqual(cfg.listen_port, 9999)
        self.assertEqual(cfg.strategy, "random")
        self.assertEqual(cfg.timeout, 1.5)
        self.assertEqual(cfg.max_retries, 0)

    def test_backends_override(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(backends="a:1,b:2"))
        self.assertEqual([b.address for b in cfg.backends], ["a:1", "b:2"])

    def test_quiet_turns_off_stdout(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(quiet=True))
        self.assertFalse(cfg.log_stdout)

    def test_verbose_flag(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(verbose=True))
        self.assertTrue(cfg.verbose)

    def test_health_overrides(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(
            health_interval=7.0, health_timeout=2.0, health_path="/ping",
            no_health_check=True,
        ))
        self.assertEqual(cfg.health_check.interval, 7.0)
        self.assertEqual(cfg.health_check.timeout, 2.0)
        self.assertEqual(cfg.health_check.path, "/ping")
        self.assertFalse(cfg.health_check.enabled)

    def test_rate_limit_enables_and_sets_rate(self):
        cfg = self.base()
        apply_cli_overrides(cfg, Args(rate_limit=20.0, rate_burst=50))
        self.assertTrue(cfg.rate_limit.enabled)
        self.assertEqual(cfg.rate_limit.refill_rate, 20.0)
        self.assertEqual(cfg.rate_limit.capacity, 50)

    def test_empty_string_treated_as_not_given(self):
        # argparse 传空串等价于没传，不能把配置覆盖成空值
        cfg = self.base()
        apply_cli_overrides(cfg, Args(listen_host=""))
        self.assertEqual(cfg.listen_host, "0.0.0.0")


class TestValidate(unittest.TestCase):

    def valid(self, **kwargs) -> ProxyConfig:
        cfg = ProxyConfig(backends=[Backend("127.0.0.1", 1)], **kwargs)
        return cfg

    def test_valid_config_passes(self):
        validate(self.valid())

    def test_no_backends_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            validate(ProxyConfig(backends=[]))
        self.assertIn("未配置任何后端", str(ctx.exception))

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(ConfigError):
            validate(self.valid(strategy="magic"))

    def test_port_out_of_range_rejected(self):
        with self.assertRaises(ConfigError):
            validate(self.valid(listen_port=0))
        with self.assertRaises(ConfigError):
            validate(self.valid(listen_port=70000))

    def test_non_positive_timeout_rejected(self):
        with self.assertRaises(ConfigError):
            validate(self.valid(timeout=0))
        with self.assertRaises(ConfigError):
            validate(self.valid(timeout=-1))

    def test_negative_max_retries_rejected(self):
        with self.assertRaises(ConfigError):
            validate(self.valid(max_retries=-1))

    def test_zero_max_retries_allowed(self):
        validate(self.valid(max_retries=0))  # 不重试是合法选择

    def test_health_interval_must_be_positive(self):
        cfg = self.valid()
        cfg.health_check.interval = 0
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_health_timeout_must_not_exceed_interval(self):
        # 探测还没超时下一轮就开始了，会堆积探测线程
        cfg = self.valid()
        cfg.health_check.timeout = 5.0
        cfg.health_check.interval = 3.0
        with self.assertRaises(ConfigError) as ctx:
            validate(cfg)
        self.assertIn("不应大于", str(ctx.exception))

    def test_health_thresholds_must_be_positive(self):
        cfg = self.valid()
        cfg.health_check.unhealthy_threshold = 0
        with self.assertRaises(ConfigError):
            validate(cfg)
        cfg = self.valid()
        cfg.health_check.healthy_threshold = 0
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_health_path_must_start_with_slash(self):
        cfg = self.valid()
        cfg.health_check.path = "health"
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_health_path_not_checked_when_disabled(self):
        cfg = self.valid()
        cfg.health_check.enabled = False
        cfg.health_check.path = "health"
        validate(cfg)  # 关掉了就不该再挑剔路径

    def test_rate_limit_bounds(self):
        cfg = self.valid()
        cfg.rate_limit.enabled = True
        cfg.rate_limit.capacity = 0
        with self.assertRaises(ConfigError):
            validate(cfg)

        cfg = self.valid()
        cfg.rate_limit.enabled = True
        cfg.rate_limit.refill_rate = 0
        with self.assertRaises(ConfigError):
            validate(cfg)

    def test_rate_limit_ignored_when_disabled(self):
        cfg = self.valid()
        cfg.rate_limit.capacity = 0        # 关掉时不该校验
        validate(cfg)


class TestBuildConfig(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_cli_only(self):
        cfg = build_config(Args(backends="127.0.0.1:1", listen_port=8123))
        self.assertEqual(cfg.listen_port, 8123)
        self.assertEqual(len(cfg.backends), 1)

    def test_file_then_cli_overrides(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"listen_port": 8000, "strategy": "random",
                       "backends": ["127.0.0.1:1"]}, f)
        cfg = build_config(Args(config=self.path, listen_port=8123))
        self.assertEqual(cfg.listen_port, 8123)     # 命令行赢
        self.assertEqual(cfg.strategy, "random")    # 配置文件保留
        self.assertEqual(len(cfg.backends), 1)

    def test_cli_backends_override_file(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"backends": ["a:1", "b:2"]}, f)
        cfg = build_config(Args(config=self.path, backends="c:3"))
        self.assertEqual([b.address for b in cfg.backends], ["c:3"])

    def test_missing_backends_raises(self):
        with self.assertRaises(ConfigError):
            build_config(Args())


class TestConfigToDict(unittest.TestCase):

    def test_round_trip(self):
        original = ProxyConfig(
            listen_host="127.0.0.1", listen_port=8123, strategy="weighted_random",
            timeout=2.5, max_retries=1, backends=parse_backends("a:1,b:2:3"),
            log_file="x.log",
        )
        original.health_check.interval = 4.0
        original.rate_limit.enabled = True

        restored = from_dict(config_to_dict(original))
        self.assertEqual(config_to_dict(restored), config_to_dict(original))

    def test_json_serializable(self):
        cfg = ProxyConfig(backends=parse_backends("127.0.0.1:1"))
        # 不能抛异常，且中文不被转义
        text = json.dumps(config_to_dict(cfg), ensure_ascii=False)
        self.assertIn("round_robin", text)

    def test_excludes_runtime_state(self):
        cfg = ProxyConfig(backends=[Backend("h", 1, 5)])
        cfg.backends[0].total_requests = 99
        cfg.backends[0].healthy = False
        data = config_to_dict(cfg)
        # 运行时状态不该混进「生效配置」，否则 --print-config 的输出会飘
        self.assertEqual(data["backends"][0], {"host": "h", "port": 1, "weight": 5})


if __name__ == "__main__":
    unittest.main()
