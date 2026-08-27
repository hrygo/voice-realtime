"""测试会议说话人时序平滑与短片段噪声滤波（DiarizationSmoother）。"""

from uuid import uuid4

from voice_realtime.meeting.diarization_smoother import DiarizationSmoother
from voice_realtime.meeting.models import NormalizedSegment, TranscriptWindow


def _make_segment(
    order: int,
    speaker: str,
    start_ms: int,
    end_ms: int,
    text: str,
    epoch: int = 1,
) -> NormalizedSegment:
    return NormalizedSegment(
        id=uuid4(),
        order=order,
        source_epoch=epoch,
        speaker_key=speaker,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
    )


def test_diarization_smoother_disabled() -> None:
    smoother = DiarizationSmoother(enabled=False)
    seg1 = _make_segment(0, "speaker:s0", 0, 100, "...")
    window = TranscriptWindow(source_epoch=1, partial="...", segments=(seg1,))
    result = smoother.smooth_window(window)
    assert result == window


def test_diarization_smoother_preserves_partial_speaker_identity() -> None:
    window = TranscriptWindow(
        source_epoch=1,
        partial="正在说",
        partial_speaker_key="epoch:1:speaker:1",
        partial_speaker_name="主持人",
        segments=(_make_segment(0, "speaker:s0", 0, 1000, "已确认"),),
    )

    smoothed = DiarizationSmoother().smooth_window(window)

    assert smoothed.partial_speaker_key == "epoch:1:speaker:1"
    assert smoothed.partial_speaker_name == "主持人"


def test_diarization_smoother_filter_short_noise() -> None:
    smoother = DiarizationSmoother(min_duration_ms=350)
    # seg1: 100ms 纯符号杂音 -> 过滤
    seg1 = _make_segment(0, "speaker:s0", 0, 100, "......")
    # seg2: 100ms 有实质文本 -> 保留
    seg2 = _make_segment(1, "speaker:s0", 200, 300, "好")
    # seg3: 500ms 正常句子 -> 保留
    seg3 = _make_segment(2, "speaker:s1", 500, 1000, "今天开会。")

    window = TranscriptWindow(source_epoch=1, partial="", segments=(seg1, seg2, seg3))
    smoothed = smoother.smooth_window(window)

    assert len(smoothed.segments) == 2
    assert smoothed.segments[0].text == "好"
    assert smoothed.segments[0].order == 0
    assert smoothed.segments[1].text == "今天开会。"
    assert smoothed.segments[1].order == 1


def test_diarization_smoother_aba_flicker_correction() -> None:
    smoother = DiarizationSmoother(min_duration_ms=350, hangover_gap_ms=1000)
    # A -> B -> A 模式，中间 B 只有 200ms
    seg_a1 = _make_segment(0, "speaker:s0", 0, 1000, "我们先看一下第一个方案")
    seg_b = _make_segment(1, "speaker:s1", 1100, 1300, "嗯对")  # 短暂被误识为 s1
    seg_a2 = _make_segment(2, "speaker:s0", 1400, 2500, "这个方案的具体细节。")

    window = TranscriptWindow(source_epoch=1, segments=(seg_a1, seg_b, seg_a2))
    smoothed = smoother.smooth_window(window)

    # 经过 A-B-A 纠偏后全部成为 speaker:s0，且时间相近被合并为一个完整段落
    assert len(smoothed.segments) == 1
    assert smoothed.segments[0].speaker_key == "speaker:s0"
    assert "我们先看一下第一个方案" in smoothed.segments[0].text
    assert "这个方案的具体细节。" in smoothed.segments[0].text
    assert smoothed.segments[0].start_ms == 0
    assert smoothed.segments[0].end_ms == 2500


def test_diarization_smoother_same_speaker_merging() -> None:
    smoother = DiarizationSmoother(hangover_gap_ms=800)
    # 两个同说话人段落，间隙 200ms <= 800ms
    seg1 = _make_segment(0, "speaker:s0", 0, 1000, "大家请看屏幕，")
    seg2 = _make_segment(1, "speaker:s0", 1200, 2000, "这是第一版设计。")
    # 间隙 1200ms > 800ms，不应合并
    seg3 = _make_segment(2, "speaker:s0", 3200, 4000, "接下来是第二部分。")

    window = TranscriptWindow(source_epoch=1, segments=(seg1, seg2, seg3))
    smoothed = smoother.smooth_window(window)

    assert len(smoothed.segments) == 2
    assert smoothed.segments[0].text == "大家请看屏幕，这是第一版设计。"
    assert smoothed.segments[0].start_ms == 0
    assert smoothed.segments[0].end_ms == 2000
    assert smoothed.segments[0].order == 0

    assert smoothed.segments[1].text == "接下来是第二部分。"
    assert smoothed.segments[1].start_ms == 3200
    assert smoothed.segments[1].order == 1


def test_diarization_smoother_latin_text_spacing() -> None:
    smoother = DiarizationSmoother(hangover_gap_ms=500)
    seg1 = _make_segment(0, "speaker:s0", 0, 1000, "Hello")
    seg2 = _make_segment(1, "speaker:s0", 1100, 2000, "world")

    window = TranscriptWindow(source_epoch=1, segments=(seg1, seg2))
    smoothed = smoother.smooth_window(window)

    assert len(smoothed.segments) == 1
    assert smoothed.segments[0].text == "Hello world"


def test_diarization_smoother_abba_flicker_correction() -> None:
    smoother = DiarizationSmoother(min_duration_ms=350, hangover_gap_ms=1000)
    # A -> B -> B -> A 模式，中间两段 B 共 300ms
    seg_a1 = _make_segment(0, "speaker:s0", 0, 1000, "第一阶段的")
    seg_b1 = _make_segment(1, "speaker:s1", 1050, 1200, "核心")
    seg_b2 = _make_segment(2, "speaker:s1", 1210, 1350, "工作是")
    seg_a2 = _make_segment(3, "speaker:s0", 1400, 2500, "治理说话人漂移。")

    window = TranscriptWindow(source_epoch=1, segments=(seg_a1, seg_b1, seg_b2, seg_a2))
    smoothed = smoother.smooth_window(window)

    assert len(smoothed.segments) == 1
    assert smoothed.segments[0].speaker_key == "speaker:s0"
    assert "第一阶段的核心工作是治理说话人漂移。" in smoothed.segments[0].text


def test_diarization_smoother_cross_epoch_merging() -> None:
    smoother = DiarizationSmoother(hangover_gap_ms=800)
    # 两个同说话人段落跨 source_epoch，间隙 100ms
    seg1 = _make_segment(0, "epoch:0:speaker:0", 0, 1000, "第一句在断线前，", epoch=0)
    seg2 = _make_segment(1, "epoch:0:speaker:0", 1100, 2000, "第二句在重连后。", epoch=1)

    window = TranscriptWindow(source_epoch=1, segments=(seg1, seg2))
    smoothed = smoother.smooth_window(window)

    assert len(smoothed.segments) == 1
    assert smoothed.segments[0].text == "第一句在断线前，第二句在重连后。"
    assert smoothed.segments[0].start_ms == 0
    assert smoothed.segments[0].end_ms == 2000

