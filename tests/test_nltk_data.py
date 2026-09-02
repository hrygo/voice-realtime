"""ensure_punkt_tab 自检安装逻辑测试：幂等与失败路径。"""

from __future__ import annotations

from unittest.mock import patch

from sona.interaction import nltk_data


def test_skips_download_when_installed() -> None:
    with (
        patch.object(nltk_data, "_is_installed", return_value=True),
        patch.object(nltk_data, "_install") as mock_install,
    ):
        assert nltk_data.ensure_punkt_tab() is True
    mock_install.assert_not_called()


def test_installs_when_missing() -> None:
    with (
        patch.object(nltk_data, "_is_installed", side_effect=[False, True]),
        patch.object(nltk_data, "_install") as mock_install,
    ):
        assert nltk_data.ensure_punkt_tab() is True
    mock_install.assert_called_once_with()


def test_returns_false_on_install_failure() -> None:
    with (
        patch.object(nltk_data, "_is_installed", side_effect=[False, False]),
        patch.object(nltk_data, "_install", side_effect=OSError("network down")),
    ):
        assert nltk_data.ensure_punkt_tab() is False


def test_returns_false_if_install_leaves_nothing() -> None:
    with (
        patch.object(nltk_data, "_is_installed", return_value=False),
        patch.object(nltk_data, "_install"),
    ):
        assert nltk_data.ensure_punkt_tab() is False


def test_install_downloads_and_extracts() -> None:
    from pathlib import Path

    with (
        patch.object(nltk_data, "_download") as mock_download,
        patch("sona.interaction.nltk_data.zipfile.ZipFile") as mock_zip,
    ):
        nltk_data._install()
    mock_download.assert_called_once()
    zip_path = mock_download.call_args.args[0]
    assert isinstance(zip_path, Path)
    assert zip_path.suffix == ".zip"
    mock_zip.assert_called_once_with(zip_path)
