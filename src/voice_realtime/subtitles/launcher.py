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
from voice_realtime.logging import setup_logging

logger = logging.getLogger(__name__)


def resolve_wlk_command(repo: Path | None = None) -> str:
    """定位 wlk 可执行文件：优先仓库 venv，其次系统 PATH。"""
    if repo is not None:
        repo_wlk = repo / ".venv" / "bin" / "wlk"
        if repo_wlk.exists():
            return str(repo_wlk)
    wlk = shutil.which("wlk")
    if wlk is None:
        raise RuntimeError("未找到 wlk 命令，请先安装 WhisperLiveKit")
    return wlk


def build_server_argv(settings: SubtitleSettings, executable: str = "wlk") -> list[str]:
    """构造 wlk serve 命令行参数。"""
    argv = [
        executable,
        "serve",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--backend",
        settings.backend,
        "--language",
        settings.language,
        "--pcm-input",
    ]
    if settings.model_dir and settings.model_dir.exists():
        argv += ["--model_dir", str(settings.model_dir)]
    elif settings.allow_model_downloads:
        argv += ["--model", settings.model_size]
    else:
        raise FileNotFoundError(
            f"字幕本地模型目录不存在: {settings.model_dir}；"
            "请准备本地模型，或显式设置 allow_model_downloads=True"
        )
    if getattr(settings, "diarization", False):
        argv += ["--diarization"]
        backend = str(getattr(settings, "diarization_backend", "sortformer"))
        argv += ["--diarization-backend", backend]
        model_path = getattr(settings, "diarization_model_path", None)
        if model_path is not None:
            path = Path(model_path)
            if not path.exists() and not settings.allow_model_downloads:
                raise FileNotFoundError(
                    f"diarization 本地模型不存在: {path}；"
                    "请准备 Sortformer 模型，或显式设置 allow_model_downloads=True"
                )
            if path.exists():
                argv += ["--sortformer-model-path", str(path)]
        elif not settings.allow_model_downloads:
            raise FileNotFoundError(
                "diarization 未配置本地模型；请准备 Sortformer 模型，或显式设置 "
                "allow_model_downloads=True"
            )
        max_speakers = int(getattr(settings, "diarization_max_speakers", 4))
        if not 1 <= max_speakers <= 4:
            raise ValueError("diarization_max_speakers 必须在 1 到 4 之间")
        argv += ["--sortformer-max-speakers", str(max_speakers)]
        argv += ["--retention-seconds", "0"]
    return argv


def install_deps(repo: Path) -> None:
    """在 WhisperLiveKit 仓库内安装后端依赖（优先仓库 venv）。"""
    python = str(repo / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable
    subprocess.run(
        [python, "-m", "pip", "install", "-e", "."],
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
        return resolve_wlk_command(settings.repo_path)
    except RuntimeError:
        logger.info("未找到 wlk，正在安装 WhisperLiveKit 依赖…")
        install_deps(settings.repo_path)
        return resolve_wlk_command(settings.repo_path)


def launch_subtitles(settings: SubtitleSettings, log_dir: Path) -> subprocess.Popen[str]:
    """启动字幕服务子进程，stdout/stderr 落盘到 log_dir。"""
    executable = resolve_wlk_command(settings.repo_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    argv = build_server_argv(settings, executable=executable)
    logger.info("启动字幕服务: %s", " ".join(argv))
    stdout_path = log_dir / "subtitles.out.log"
    stderr_path = log_dir / "subtitles.err.log"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
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

    setup_logging()
    settings = get_settings().subtitles
    log_dir = settings.output_dir
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
