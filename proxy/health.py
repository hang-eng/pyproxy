"""后端健康检查。

一个后台守护线程周期性探测所有后端，维护每个后端的状态机：

    HEALTHY   --(连续失败 unhealthy_threshold 次)-->  UNHEALTHY
    UNHEALTHY --(连续成功 healthy_threshold   次)-->  HEALTHY

为什么要求「连续」而不是「一次」：网络抖动、后端 GC 停顿都可能造成单次
探测失败。若一次失败就剔除、一次成功就恢复，状态会反复横跳（flapping），
流量也跟着来回甩。用连续计数把这个抖动过滤掉。

两个约定：

* 后端初始状态是「健康」（乐观启动），这样代理启动后立刻可用，
  不必等第一轮探测跑完。若后端其实是坏的，会在一个检查周期内被剔除。
* 后端从「不健康」恢复时把 current_weight 归零。否则它可能带着挂掉之前
  累积的加权值回来，被平滑加权轮询连续选中好几次。
"""

from __future__ import annotations

import http.client
import logging
import threading
from typing import Callable, Dict, Iterable, List, Optional

from .config import Backend, HealthCheckConfig

LOG = logging.getLogger("proxy.health")


class HealthChecker:
    """周期性探测后端健康状态。

    ``check_once()`` 是同步的、可单独调用，单元测试靠它绕过线程。
    """

    def __init__(
        self,
        backends: Iterable[Backend],
        config: HealthCheckConfig,
        on_change: Optional[Callable[[Backend], None]] = None,
    ):
        self._backends: List[Backend] = list(backends)
        self._config = config
        self._on_change = on_change
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rounds = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动后台探测线程；配置里关掉健康检查时什么也不做。"""
        if not self._config.enabled:
            LOG.info("健康检查已关闭，所有后端始终视为可用")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="health-checker", daemon=True
        )
        self._thread.start()
        LOG.info(
            "健康检查已启动：周期 %.1fs，探测 %s，连续失败 %d 次剔除 / 连续成功 %d 次恢复",
            self._config.interval,
            self._config.path,
            self._config.unhealthy_threshold,
            self._config.healthy_threshold,
        )

    def stop(self, timeout: float = 2.0) -> None:
        """通知线程退出并等待其结束。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def rounds(self) -> int:
        """已完成的检查轮数（用于测试与统计）。"""
        return self._rounds

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:  # 单轮异常不能拖垮整个线程
                LOG.exception("健康检查轮次异常")
            # 用 Event.wait 而不是 time.sleep，stop() 时能立刻醒来
            self._stop.wait(self._config.interval)

    # ---------------------------------------------------------------- 检查逻辑

    def check_once(self) -> None:
        """对所有后端执行一轮探测。同步方法，便于测试。"""
        for backend in self._backends:
            if self._stop.is_set():
                return
            self.apply_result(backend, self._probe(backend))
        self._rounds += 1

    def _probe(self, backend: Backend) -> bool:
        """对单个后端发一次 HTTP 探测，返回是否健康。"""
        conn = http.client.HTTPConnection(
            backend.host, backend.port, timeout=self._config.timeout
        )
        try:
            conn.request("GET", self._config.path)
            response = conn.getresponse()
            response.read()  # 必须读完响应体，否则连接无法正常关闭
            return 200 <= response.status < 400
        except (OSError, http.client.HTTPException):
            # 连接被拒、DNS 失败、超时都归到 OSError 下
            return False
        finally:
            conn.close()

    def apply_result(self, backend: Backend, probe_ok: bool) -> None:
        """把一次探测结果喂给状态机。

        公开方法，便于单元测试直接驱动状态迁移而不必真的发请求。
        """
        with self._lock:
            was_healthy = backend.healthy

            if probe_ok:
                backend.consecutive_failures = 0
                backend.consecutive_successes += 1
                if not was_healthy and backend.consecutive_successes >= self._config.healthy_threshold:
                    backend.healthy = True
                    backend.consecutive_successes = 0
                    backend.current_weight = 0  # 丢弃挂掉前累积的加权值
            else:
                backend.consecutive_successes = 0
                backend.consecutive_failures += 1
                if was_healthy and backend.consecutive_failures >= self._config.unhealthy_threshold:
                    backend.healthy = False

            changed = backend.healthy != was_healthy

        if changed:
            self._announce(backend)

    def _announce(self, backend: Backend) -> None:
        if backend.healthy:
            LOG.info(
                "后端恢复：%s（连续成功 %d 次）",
                backend.address,
                self._config.healthy_threshold,
            )
        else:
            LOG.warning(
                "后端剔除：%s（连续失败 %d 次）",
                backend.address,
                self._config.unhealthy_threshold,
            )
        if self._on_change is not None:
            try:
                self._on_change(backend)
            except Exception:
                LOG.exception("健康状态变更回调执行失败")

    # ---------------------------------------------------------------- 只读视图

    def snapshot(self) -> List[Dict]:
        """导出后端健康详情，供 Web 管理界面使用。"""
        with self._lock:
            return [
                {
                    "address": b.address,
                    "healthy": b.healthy,
                    "consecutive_failures": b.consecutive_failures,
                    "consecutive_successes": b.consecutive_successes,
                    "total_requests": b.total_requests,
                }
                for b in self._backends
            ]
