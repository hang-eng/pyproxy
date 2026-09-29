"""访问日志。

每条请求一行，同时可以写到 stdout 和文件：

    2026-09-29 09:41:02 | 127.0.0.1 | GET /api/user | 200 | 127.0.0.1:8001 | 12B | 3.2ms | retry=2

为什么自己写而不用 logging 模块：访问日志是**结构化的一行一条**，
要能被 grep/awk 直接处理，字段顺序和分隔符必须固定。logging 的
格式化是为「给人看的诊断信息」设计的，混在一起反而不好用。
两个通道各自独立开关：排查问题时开 stdout，长期运行写文件。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from typing import Dict, IO, Optional

# 请求没有状态码时（比如还没转发就失败）显示的占位符
PLACEHOLDER = "-"


class AccessLogger:
    """把访问记录写成一行文本，线程安全。

    实例本身是 callable，直接当回调传给 server，不必再包一层。
    """

    def __init__(
        self,
        path: Optional[str] = None,
        to_stdout: bool = True,
    ):
        self._path = path
        self._to_stdout = to_stdout
        self._lock = threading.Lock()
        self._handle: Optional[IO[str]] = None

        if path:
            self._handle = self._open(path)

    @staticmethod
    def _open(path: str) -> IO[str]:
        """打开日志文件，父目录不存在就建出来。"""
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # 追加模式：代理重启不该把历史日志清掉
        return open(path, "a", encoding="utf-8")

    # ------------------------------------------------------------------ 输出

    def __call__(self, record: Dict) -> None:
        line = self.format(record)
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.write(line + "\n")
                    # 立刻落盘：出问题时能 tail 到最新一条，
                    # 而不是等缓冲区攒满
                    self._handle.flush()
                except OSError as exc:
                    # 写日志失败不能影响请求本身。但也不能每条请求都报一次：
                    # 磁盘写满之后，每个请求往 stderr 喷一行警告，本身就成了
                    # 一种自伤。所以报一次、然后彻底放弃文件通道，退化成
                    # 只输出终端——和「文件打不开」时的行为保持一致。
                    self._handle = None
                    print(
                        f"[警告] 访问日志写入失败，后续只输出到终端：{exc}",
                        file=sys.stderr, flush=True,
                    )
            if self._to_stdout:
                try:
                    print(line, flush=True)
                except OSError:
                    # stdout 的读者先退出了（比如 `... | head`），
                    # 这不是错误，也没什么可做的
                    pass

    @staticmethod
    def format(record: Dict) -> str:
        """把一个记录字典格式化成一行。"""
        status = record.get("status") or PLACEHOLDER
        size = record.get("bytes")
        size_text = f"{size}B" if isinstance(size, int) else PLACEHOLDER
        retries = record.get("retries") or 0
        retry_text = f"retry={retries}" if retries else PLACEHOLDER

        return " | ".join(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                str(record.get("client") or PLACEHOLDER),
                f"{record.get('method', PLACEHOLDER)} {record.get('path', PLACEHOLDER)}",
                str(status),
                str(record.get("backend") or PLACEHOLDER),
                size_text,
                f"{record.get('duration_ms', 0.0):.1f}ms",
                retry_text,
            ]
        )

    # ------------------------------------------------------------------ 收尾

    @property
    def path(self) -> Optional[str]:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    # 关闭时的 flush 失败（比如磁盘写满）已经无法补救：
                    # 收尾阶段报错只会污染退出时的输出
                    pass
                self._handle = None

    def __enter__(self) -> "AccessLogger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
