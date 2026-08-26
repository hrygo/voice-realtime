"""共享日志配置：统一格式、控制台与滚动文件输出、环境变量动态配置与第三方降噪。"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_DIR = Path("runtime/logs")
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUP_COUNT = 5

NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "urllib3",
    "asyncio",
    "nemo_logger",
    "nv_one_logger",
    "transformers",
)


def _parse_bool_env(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_level(level: int | str | None) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str) and level.strip():
        name = level.strip().upper()
        if name in logging.getLevelNamesMapping():
            return logging.getLevelNamesMapping()[name]
    env_level = os.environ.get("VR_LOG_LEVEL", "").strip().upper()
    if env_level and env_level in logging.getLevelNamesMapping():
        return logging.getLevelNamesMapping()[env_level]
    return logging.INFO


def setup_logging(
    service_name_or_level: str | int = "voice-realtime",
    *,
    level: int | str | None = None,
    log_dir: Path | str | None = None,
    log_file: Path | str | None = None,
    enable_file: bool | None = None,
    enable_console: bool | None = None,
    apply: bool = True,
) -> None:
    """初始化 root logger。

    Args:
        service_name_or_level: 服务名称（如 "ui", "bridge"）或旧接口传入的日志级别。
        level: 日志级别（优先于环境变量 `VR_LOG_LEVEL`）。
        log_dir: 日志输出目录（默认读取 `VR_LOG_DIR` 或 `runtime/logs`）。
        log_file: 显式指定日志文件绝对/相对路径（优先于 `log_dir/{service_name}.log`）。
        enable_file: 是否启用文件落盘（默认读取 `VR_LOG_TO_FILE`，默认 True）。
        enable_console: 是否启用控制台输出（默认读取 `VR_LOG_TO_CONSOLE`，默认 True）。
        apply: 是否实际应用配置（测试环境传 False 避免干扰 pytest 捕获）。
    """
    if not apply:
        return

    if isinstance(service_name_or_level, int):
        service_name = "voice-realtime"
        resolved_level = service_name_or_level if level is None else _resolve_level(level)
    else:
        service_name = service_name_or_level
        resolved_level = _resolve_level(level)

    to_console = (
        _parse_bool_env("VR_LOG_TO_CONSOLE", True)
        if enable_console is None
        else enable_console
    )
    to_file = (
        _parse_bool_env("VR_LOG_TO_FILE", True)
        if enable_file is None
        else enable_file
    )

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)

    # 移除此前由 setup_logging 挂载的旧 handler，避免重复打印
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    if to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(resolved_level)
        root_logger.addHandler(console_handler)

    if to_file:
        if log_file is not None:
            target_path = Path(log_file)
        else:
            env_dir = os.environ.get("VR_LOG_DIR")
            target_dir = (
                Path(log_dir)
                if log_dir is not None
                else (Path(env_dir) if env_dir else DEFAULT_LOG_DIR)
            )
            target_path = target_dir / f"{service_name}.log"

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                target_path,
                maxBytes=DEFAULT_MAX_BYTES,
                backupCount=DEFAULT_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(resolved_level)
            root_logger.addHandler(file_handler)
        except OSError:
            # 文件创建或权限受限时退化，仅靠 console 输出，不阻塞系统启动
            pass

    # 降噪第三方高频日志
    noisy_level = logging.INFO if resolved_level <= logging.DEBUG else logging.WARNING
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(noisy_level)
