"""WhisperLiveKit 字幕服务启动器。

负责：校验/安装 WhisperLiveKit（qwen3-streaming 后端）、
构造 wlk serve 命令、以子进程方式启动并落盘日志。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from voice_realtime.config import SubtitleSettings

logger = logging.getLogger(__name__)

_BACKEND_EXTRAS: dict[str, str] = {
    "qwen3-streaming": "qwen3-streaming",
    "funasr": "funasr",
    "auto": "",
}


def resolve_wlk_command() -> str:
    """定位 wlk 可执行文件。"""
    wlk = shutil.which("wlk")
    if wlk is None:
        raise RuntimeError("未找到 wlk 命令，请先安装 WhisperLiveKit")
    return wlk


def build_server_argv(settings: SubtitleSettings) -> list[str]:
    """构造 wlk serve 命令行参数。"""
    return [
        "wlk",
        "serve",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--backend",
        settings.backend,
        "--language",
        settings.language,
    ]


def install_deps(repo: Path) -> None:
    """在 WhisperLiveKit 仓库内安装后端依赖（复用当前 venv）。"""
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_whisperlivekit(settings: SubtitleSettings) -> str:
    """确保 WhisperLiveKit 可用：仓库存在 + wlk 可执行。返回 wlk 路径。"""
    if not settings.repo_path.exists():
        raise FileNotFoundError(
            f"WhisperLiveKit 仓库不存在: {settings.repo_path}（请先 git clone 到该路径）"
        )
    try:
        return resolve_wlk_command()
    except RuntimeError:
        logger.info("未找到 wlk，正在安装 WhisperLiveKit 依赖…")
        install_deps(settings.repo_path)
        return resolve_wlk_command()


def launch_subtitles(settings: SubtitleSettings, log_dir: Path) -> subprocess.Popen[str]:
    """启动字幕服务子进程，stdout/stderr 落盘到 log_dir。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    argv = build_server_argv(settings)
    logger.info("启动字幕服务: %s", " ".join(argv))
    stdout = (log_dir / "subtitles.out.log").open("w")
    stderr = (log_dir / "subtitles.err.log").open("w")
    return subprocess.Popen(
        argv,
        stdout=stdout,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )


def main() -> None:
    """`vr-subtitles` 控制台入口。"""
    import signal
    import time

    from voice_realtime.config import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings().subtitles
    log_dir = Path("runtime") / "subtitles"
    prepare_whisperlivekit(settings)
    proc = launch_subtitles(settings, log_dir)

    def _stop(_sig: int, _frame: object) -> None:
        logger.info("收到信号，停止字幕服务 (pid=%d)", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        _stop(signal.SIGINT, None)
    logger.info("字幕服务退出 (code=%s)", proc.returncode)


if __name__ == "__main__":
    main()
