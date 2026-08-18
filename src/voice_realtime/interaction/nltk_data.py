"""NLTK punkt_tab 数据自检与安装。

pipecat 1.7 的 TTS 断句依赖 ``nltk.tokenize.sent_tokenize``，需要
``tokenizers/punkt_tab`` 资源；缺失时 OpenAITTSService 每轮抛 ErrorFrame。
本环境 nltk 默认下载源会被网络策略拦截，故手动经 raw.githubusercontent.com
下载（实测可达，约 3.5s）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

PUNKT_TAB_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/"
    "packages/tokenizers/punkt_tab.zip"
)
_TOKENIZERS_DIR = Path.home() / "nltk_data" / "tokenizers"


def _is_installed() -> bool:
    return (_TOKENIZERS_DIR / "punkt_tab" / "english").is_dir()


def _install() -> None:
    zip_path = _TOKENIZERS_DIR / "punkt_tab.zip"
    _TOKENIZERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _download(zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(_TOKENIZERS_DIR)
    finally:
        zip_path.unlink(missing_ok=True)


def _download(target: Path) -> None:
    if shutil.which("curl") is not None:
        subprocess.run(
            ["curl", "-fsSL", "-o", str(target), PUNKT_TAB_URL],
            check=True,
            capture_output=True,
            timeout=90,
        )
    else:
        urllib.request.urlretrieve(PUNKT_TAB_URL, target)


def ensure_punkt_tab() -> bool:
    """确保 punkt_tab 可用；已就绪或安装成功返回 True，安装失败返回 False。"""
    if _is_installed():
        return True
    logger.info("NLTK punkt_tab 缺失，开始下载安装…")
    try:
        _install()
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        logger.warning("punkt_tab 自动安装失败，运行 scripts/install-nltk-data.sh 手动安装")
        return False
    if _is_installed():
        logger.info("NLTK punkt_tab 安装完成")
        return True
    return False
