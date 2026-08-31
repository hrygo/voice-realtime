"""会议转录说话人时序平滑与短片段噪声滤波。

针对流式说话人分离（Sortformer）在真实会议拾音中的局部跳变与碎片化问题：
1. 过滤极短无意义噪音片段；
2. 纠偏 A-B-A 短暂说话人闪烁（Hangover Smoothing）；
3. 合并相邻同说话人时间邻近分段。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow

# 匹配是否含有实质内容（汉字、字母、数字）
_MEANINGFUL_CHAR_RE = re.compile(r"[\u4e00-\u9fa5a-zA-Z0-9]")


def _has_meaningful_text(text: str) -> bool:
    return bool(_MEANINGFUL_CHAR_RE.search(text))


def _join_text(text1: str, text2: str) -> str:
    """合并两段文本，若两端均为英文/数字则补充空格，中文直接连接。"""
    t1 = text1.strip()
    t2 = text2.strip()
    if not t1:
        return t2
    if not t2:
        return t1
    last_char = t1[-1]
    first_char = t2[0]
    if (last_char.isalnum() and not '\u4e00' <= last_char <= '\u9fa5') and (
        first_char.isalnum() and not '\u4e00' <= first_char <= '\u9fa5'
    ):
        return f"{t1} {t2}"
    return f"{t1}{t2}"


class DiarizationSmoother:
    """会议说话人分离时序平滑器。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_duration_ms: int = 350,
        hangover_gap_ms: int = 1000,
    ) -> None:
        self.enabled = enabled
        self.min_duration_ms = min_duration_ms
        self.hangover_gap_ms = hangover_gap_ms

    def smooth_window(self, window: TranscriptWindow) -> TranscriptWindow:
        """对 TranscriptWindow 中的 segments 执行平滑处理。"""
        if not self.enabled or not window.segments:
            return window

        smoothed_segments = tuple(
            self.smooth_segments(
                window.segments,
                min_duration_ms=self.min_duration_ms,
                hangover_gap_ms=self.hangover_gap_ms,
            )
        )

        if smoothed_segments == window.segments:
            return window

        return TranscriptWindow(
            source_epoch=window.source_epoch,
            partial=window.partial,
            partial_speaker_key=window.partial_speaker_key,
            partial_speaker_name=window.partial_speaker_name,
            segments=smoothed_segments,
            speaker_remap=window.speaker_remap,
        )

    @classmethod
    def smooth_segments(
        cls,
        segments: Sequence[NormalizedSegment],
        *,
        min_duration_ms: int = 350,
        hangover_gap_ms: int = 1000,
    ) -> list[NormalizedSegment]:
        """对分段序列进行短噪声过滤、A-B-A 纠偏和同说话人合并。"""
        if not segments:
            return []

        # 步骤 1：过滤无实质意义的极短孤立片段
        filtered: list[NormalizedSegment] = []
        for segment in segments:
            duration = segment.end_ms - segment.start_ms
            if duration < min_duration_ms and not _has_meaningful_text(segment.text):
                continue
            filtered.append(segment)

        if not filtered:
            return []

        # 步骤 2：说话人闪烁纠偏（单片段 A-B-A 及双片段 A-B-B-A 短时突变）
        n = len(filtered)
        speaker_keys = [seg.speaker_key for seg in filtered]

        # 2a. 单片段 A-B-A 纠偏
        for i in range(1, n - 1):
            prev_spk = speaker_keys[i - 1]
            curr_spk = speaker_keys[i]
            next_spk = speaker_keys[i + 1]
            curr_dur = filtered[i].end_ms - filtered[i].start_ms
            if (
                prev_spk == next_spk
                and curr_spk != prev_spk
                and curr_dur <= max(min_duration_ms, 500)
            ):
                speaker_keys[i] = prev_spk

        # 2b. 双片段 A-B-B-A 纠偏
        for i in range(1, n - 2):
            prev_spk = speaker_keys[i - 1]
            b1_spk = speaker_keys[i]
            b2_spk = speaker_keys[i + 1]
            next_spk = speaker_keys[i + 2]
            b_total_dur = filtered[i + 1].end_ms - filtered[i].start_ms
            if (
                prev_spk == next_spk
                and b1_spk == b2_spk
                and b1_spk != prev_spk
                and b_total_dur <= max(min_duration_ms, 600)
            ):
                speaker_keys[i] = prev_spk
                speaker_keys[i + 1] = prev_spk

        corrected_segments: list[NormalizedSegment] = []
        for i, seg in enumerate(filtered):
            new_spk = speaker_keys[i]
            if new_spk != seg.speaker_key:
                corrected_segments.append(
                    NormalizedSegment(
                        id=seg.id,
                        order=seg.order,
                        source_epoch=seg.source_epoch,
                        speaker_key=new_spk,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        text=seg.text,
                        translation=seg.translation,
                        detected_language=seg.detected_language,
                    )
                )
            else:
                corrected_segments.append(seg)

        # 步骤 3：合并相邻同说话人且时间间隙在 hangover_gap_ms 内的片段（允许跨重连 epoch 合并）
        merged: list[NormalizedSegment] = []
        for seg in corrected_segments:
            if not merged:
                merged.append(seg)
                continue

            last = merged[-1]
            gap = seg.start_ms - last.end_ms
            if (
                last.speaker_key == seg.speaker_key
                and gap <= hangover_gap_ms
            ):
                # 合并 last 与 seg
                merged_text = _join_text(last.text, seg.text)
                merged[-1] = NormalizedSegment(
                    id=last.id,
                    order=last.order,
                    source_epoch=last.source_epoch,
                    speaker_key=last.speaker_key,
                    start_ms=last.start_ms,
                    end_ms=max(last.end_ms, seg.end_ms),
                    text=merged_text,
                    translation=(
                        f"{last.translation or ''} {seg.translation or ''}".strip() or None
                    ),
                    detected_language=last.detected_language or seg.detected_language,
                )
            else:
                merged.append(seg)

        # 步骤 4：重新规范化序号 order
        result: list[NormalizedSegment] = []
        for index, seg in enumerate(merged):
            if seg.order != index:
                result.append(
                    NormalizedSegment(
                        id=seg.id,
                        order=index,
                        source_epoch=seg.source_epoch,
                        speaker_key=seg.speaker_key,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        text=seg.text,
                        translation=seg.translation,
                        detected_language=seg.detected_language,
                    )
                )
            else:
                result.append(seg)

        return result
