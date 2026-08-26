"""日志配置模块单元测试。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from voice_realtime.logging import (
    NOISY_LOGGERS,
    SanitizingFilter,
    _parse_bool_env,
    _resolve_level,
    setup_logging,
)


@pytest.fixture(autouse=True)
def clean_root_logger() -> None:
    """每个测试前后保存并恢复 root logger 状态。"""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)


def test_parse_bool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOL", "1")
    assert _parse_bool_env("TEST_BOOL", False) is True
    monkeypatch.setenv("TEST_BOOL", "true")
    assert _parse_bool_env("TEST_BOOL", False) is True
    monkeypatch.setenv("TEST_BOOL", "0")
    assert _parse_bool_env("TEST_BOOL", True) is False
    monkeypatch.setenv("TEST_BOOL", "false")
    assert _parse_bool_env("TEST_BOOL", True) is False
    assert _parse_bool_env("NON_EXISTENT_KEY", True) is True


def test_resolve_level(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _resolve_level(logging.DEBUG) == logging.DEBUG
    assert _resolve_level("WARNING") == logging.WARNING
    assert _resolve_level("debug") == logging.DEBUG

    monkeypatch.setenv("VR_LOG_LEVEL", "ERROR")
    assert _resolve_level(None) == logging.ERROR

    monkeypatch.delenv("VR_LOG_LEVEL", raising=False)
    assert _resolve_level(None) == logging.INFO


def test_setup_logging_basic(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging("test-svc", log_dir=log_dir, level=logging.INFO)

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(root.handlers) == 2  # StreamHandler + RotatingFileHandler

    file_handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
    assert file_handler.baseFilename == str(log_dir / "test-svc.log")

    logger = logging.getLogger("test.module")
    logger.info("测试一条信息")
    file_handler.flush()

    content = Path(file_handler.baseFilename).read_text(encoding="utf-8")
    assert "INFO test.module 测试一条信息" in content


def test_setup_logging_sanitizes_credentials(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging("test-svc", log_dir=log_dir, level=logging.INFO)

    file_handler = next(
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    )
    logger = logging.getLogger("test.security")
    logger.info("连接数据库: postgresql://my_user:SuperSecretPass123@127.0.0.1:5432/knowledge")
    logger.info("授权信息: %s", "Bearer my-secret-token-xyz-12345")
    logger.info("配置项: 'password': 'my-raw-password'")
    file_handler.flush()

    content = Path(file_handler.baseFilename).read_text(encoding="utf-8")
    assert "SuperSecretPass123" not in content
    assert "postgresql://my_user:***@127.0.0.1:5432/knowledge" in content
    assert "my-secret-token-xyz" not in content
    assert "Bearer ***" in content
    assert "'password': '***'" in content


def test_sanitizing_filter_direct() -> None:
    f = SanitizingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="DSN: postgres://admin:p@ssw0rd@localhost/db, token: Bearer abc.123",
        args=(),
        exc_info=None,
    )
    assert f.filter(record) is True
    assert record.msg == "DSN: postgres://admin:***@localhost/db, token: Bearer ***"

    record_with_args = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Connecting with %s and %s",
        args=("postgres://admin:secret@localhost/db", "password='secret'"),
        exc_info=None,
    )
    assert f.filter(record_with_args) is True
    assert record_with_args.args == ("postgres://admin:***@localhost/db", "password='***'")


def test_setup_logging_disable_file(tmp_path: Path) -> None:
    setup_logging("test-svc", log_dir=tmp_path, enable_file=False)
    root = logging.getLogger()
    assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)


def test_setup_logging_disable_console(tmp_path: Path) -> None:
    setup_logging("test-svc", log_dir=tmp_path, enable_console=False)
    root = logging.getLogger()
    assert not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root.handlers
    )


def test_setup_logging_explicit_file(tmp_path: Path) -> None:
    custom_file = tmp_path / "custom" / "my.log"
    setup_logging("test-svc", log_file=custom_file)
    root = logging.getLogger()
    file_handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
    assert file_handler.baseFilename == str(custom_file)


def test_setup_logging_legacy_signature() -> None:
    setup_logging(logging.WARNING, apply=True, enable_file=False)
    root = logging.getLogger()
    assert root.level == logging.WARNING


def test_setup_logging_apply_false() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    setup_logging("test-svc", apply=False)
    assert len(root.handlers) == 0


def test_setup_logging_noisy_suppression() -> None:
    setup_logging("test-svc", level=logging.INFO, enable_file=False)
    for noisy in NOISY_LOGGERS:
        assert logging.getLogger(noisy).level == logging.WARNING

    setup_logging("test-svc", level=logging.DEBUG, enable_file=False)
    for noisy in NOISY_LOGGERS:
        assert logging.getLogger(noisy).level == logging.INFO
