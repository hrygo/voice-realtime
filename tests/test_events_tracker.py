"""字幕事件去重/增量跟踪测试。

WhisperLiveKit 每 ~0.2s 推送全量 FrontData 快照（同一 confirmed 段反复出现），
tracker 只对外发出「新 partial 文本」与「新 confirmed 段」。
"""

from __future__ import annotations

from voice_realtime.subtitles.events import SubtitleEvent, SubtitleEventTracker


class TestSubtitleEventTracker:
    def test_first_confirmed_segment_emitted(self) -> None:
        tracker = SubtitleEventTracker()
        ev = SubtitleEvent(
            kind="confirmed",
            text="你好。",
            start="00:00:00.000",
            end="00:00:00.480",
        )
        assert tracker.track(ev) is True

    def test_repeated_snapshot_deduped(self) -> None:
        tracker = SubtitleEventTracker()
        ev = SubtitleEvent(
            kind="confirmed",
            text="你好。",
            start="00:00:00.000",
            end="00:00:00.480",
        )
        tracker.track(ev)
        assert tracker.track(ev) is False  # 同一段重复推送

    def test_new_segment_after_old_emitted(self) -> None:
        tracker = SubtitleEventTracker()
        ev1 = SubtitleEvent(kind="confirmed", text="你好。", start="0:00:00.00")
        ev2 = SubtitleEvent(kind="confirmed", text="再见。", start="0:00:02.00")
        tracker.track(ev1)
        assert tracker.track(ev2) is True

    def test_partial_unchanged_deduped(self) -> None:
        tracker = SubtitleEventTracker()
        ev = SubtitleEvent(kind="partial", text="你好世")
        tracker.track(ev)
        assert tracker.track(ev) is False

    def test_partial_extended_is_new(self) -> None:
        tracker = SubtitleEventTracker()
        tracker.track(SubtitleEvent(kind="partial", text="你好世"))
        assert tracker.track(SubtitleEvent(kind="partial", text="你好世界")) is True

    def test_error_always_emitted(self) -> None:
        tracker = SubtitleEventTracker()
        ev = SubtitleEvent(kind="error", text="boom")
        tracker.track(ev)
        assert tracker.track(ev) is True

    def test_empty_partial_not_emitted(self) -> None:
        tracker = SubtitleEventTracker()
        assert tracker.track(SubtitleEvent(kind="partial", text="")) is False

    def test_old_confirmed_segment_evicted_after_max_seen(self) -> None:
        tracker = SubtitleEventTracker(max_seen=2)
        events = [
            SubtitleEvent(kind="confirmed", text=f"段落{i}", start=str(i))
            for i in range(3)
        ]

        for event in events:
            assert tracker.track(event) is True

        assert tracker.track(events[0]) is True
        assert tracker.track(events[2]) is False

    def test_small_max_seen_is_applied(self) -> None:
        tracker = SubtitleEventTracker(max_seen=2)
        first = SubtitleEvent(kind="confirmed", text="首段", start="0")
        second = SubtitleEvent(kind="confirmed", text="次段", start="1")
        third = SubtitleEvent(kind="confirmed", text="末段", start="2")

        assert tracker.track(first) is True
        assert tracker.track(second) is True
        assert tracker.track(third) is True
        assert tracker.track(first) is True
        assert tracker.track(second) is True
