"""共享日志配置：三个 CLI 入口（vr-bridge / vr-interact / vr-subtitles）统一日志格式。

此前各入口各自调用 `logging.basicConfig`（三处重复），集中到此处后
格式与级别可一处调整，测试中也可用 `apply=False` 避免污染 pytest 捕获。
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: int = logging.INFO, *, apply: bool = True) -> None:
    """初始化 root logger。

    Args:
        level: 日志级别。
        apply: 是否实际调用 basicConfig（测试环境传 False 避免干扰 pytest 捕获）。
    """
    if apply:
        logging.basicConfig(level=level, format=LOG_FORMAT)
