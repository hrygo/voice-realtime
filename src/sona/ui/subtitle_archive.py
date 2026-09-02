"""SRT 原子快照与 epoch 归档。"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


class SrtArchive:
    """管理 ``current.srt`` 原子替换、confirmed 去重与 epoch 归档。

    本类只负责文件语义，不广播 UI event，也不感知 SpeechRail 或会议状态。
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._persisted_confirmed_signature: tuple[tuple[str, ...], ...] | None = None
        self._session_has_confirmed = False
        self._archived = False

    @property
    def session_has_confirmed(self) -> bool:
        return self._session_has_confirmed

    def reset_epoch(self) -> None:
        """开启新 epoch：允许再次归档并清空 confirmed 去重状态。"""
        self._persisted_confirmed_signature = None
        self._session_has_confirmed = False
        self._archived = False

    def persist_confirmed(self, payload: Mapping[str, object]) -> None:
        """confirmed 快照变化时原子替换 current.srt；partial-only 不落盘。"""
        signature = self.confirmed_signature(payload)
        if not signature or signature == self._persisted_confirmed_signature:
            return
        output = self._render_srt(self._confirmed_lines(payload))
        self._output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._output_dir / "current.srt.tmp"
        current = self._output_dir / "current.srt"
        temporary.write_text(output, encoding="utf-8")
        temporary.replace(current)
        self._persisted_confirmed_signature = signature
        self._session_has_confirmed = True

    def close_epoch(self) -> Path | None:
        """封存当前 epoch 的 SRT；每个 epoch 最多归档一次。"""
        if self._archived or not self._session_has_confirmed:
            return None
        current = self._output_dir / "current.srt"
        if not current.is_file() or not current.stat().st_size:
            return None
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive = self._output_dir / f"session-{timestamp}.srt"
        suffix = 2
        while archive.exists():
            archive = self._output_dir / f"session-{timestamp}-{suffix}.srt"
            suffix += 1
        shutil.copy2(current, archive)
        self._archived = True
        return archive

    def clear_current(self) -> None:
        """原子清空 current.srt 并重置 confirmed 去重状态。"""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._output_dir / "current.srt.tmp"
        current = self._output_dir / "current.srt"
        temporary.write_text("", encoding="utf-8")
        temporary.replace(current)
        self._persisted_confirmed_signature = None
        self._session_has_confirmed = False

    @staticmethod
    def confirmed_signature(payload: Mapping[str, object]) -> tuple[tuple[str, ...], ...]:
        """返回 confirmed 行的不可变签名，用于广播与落盘去重。"""
        return tuple(
            (
                str(line.get("start") or ""),
                str(line.get("end") or ""),
                str(line.get("speaker") if line.get("speaker") is not None else ""),
                str(line.get("text") or ""),
            )
            for line in SrtArchive._confirmed_lines(payload)
        )

    @staticmethod
    def _confirmed_lines(payload: Mapping[str, object]) -> list[dict[str, Any]]:
        lines = payload.get("lines")
        if not isinstance(lines, list):
            return []
        return [
            line
            for line in lines
            if isinstance(line, dict)
            and str(line.get("text") or "").strip()
            and line.get("speaker") != -2
        ]

    @classmethod
    def _render_srt(cls, lines: list[dict[str, Any]]) -> str:
        blocks = []
        for index, line in enumerate(lines, start=1):
            start = cls._srt_timestamp(line.get("start"))
            end = cls._srt_timestamp(line.get("end"))
            text = str(line.get("text") or "").strip()
            blocks.append(f"{index}\n{start} --> {end}\n{text}")
        return "\n\n".join(blocks) + "\n"

    @staticmethod
    def _srt_timestamp(value: object) -> str:
        raw = str(value or "00:00:00").strip().replace(".", ",")
        clock, separator, fraction = raw.partition(",")
        parts = clock.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
        else:
            hours, minutes, seconds = "0", "0", "0"
        millis = (fraction if separator else "0").ljust(3, "0")[:3]
        return f"{hours.zfill(2)}:{minutes.zfill(2)}:{seconds.zfill(2)},{millis}"
