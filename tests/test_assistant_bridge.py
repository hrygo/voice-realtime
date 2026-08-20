"""StatusBridgeObserver 单元测试：帧→事件映射、去重、节流、广播。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    EndFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed

from voice_realtime.ui.assistant_bridge import StatusBridgeObserver


def _upsert_mock_client(observer: StatusBridgeObserver) -> AsyncMock:
    client = AsyncMock()
    observer.add_client(client)
    return client


async def _push(observer: StatusBridgeObserver, frame) -> None:
    """模拟一次 source→destination 帧传输（直接调 on_push_frame）。"""
    data = FramePushed(  # type: ignore[arg-type]
        source=None, destination=None, frame=frame, direction="downstream", timestamp=0
    )
    await observer.on_push_frame(data)


def _text_args(**overrides: str) -> dict[str, str]:
    """TextFrame 子类公共构造参数。"""
    args = {"text": "你好", "user_id": "u1", "timestamp": "t1"}
    args.update(overrides)
    return args


def _tts_audio() -> TTSAudioRawFrame:
    """构造一个 TTS 输出音频帧（24k 单声道 10ms）。"""
    return TTSAudioRawFrame(audio=b"\x00" * 320, sample_rate=24000, num_channels=1)


class TestClientManagement:
    async def test_add_remove_client(self) -> None:
        observer = StatusBridgeObserver()
        send = AsyncMock()
        observer.add_client(send)
        assert observer.has_clients
        observer.remove_client(send)
        assert not observer.has_clients


class TestEventMapping:
    async def test_transcription_final_event(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, TranscriptionFrame(**_text_args(), finalized=True))
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "stt"
        assert payload["state"] == "final"
        assert payload["text"] == "你好"

    async def test_interim_transcription_event(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, InterimTranscriptionFrame(**_text_args()))
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "stt"
        assert payload["state"] == "interim"

    async def test_llm_streaming_event_with_turn_id(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, LLMTextFrame(text="增量"))
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "llm"
        assert payload["state"] == "streaming"
        assert payload["text"] == "增量"
        assert payload["turn_id"] == 0

    async def test_llm_end_increments_turn(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, LLMTextFrame(text="第一轮"))
        await _push(observer, LLMFullResponseEndFrame())
        first, second = (json.loads(c.args[0]) for c in client.call_args_list)
        assert first["turn_id"] == 0
        assert second["type"] == "llm"
        assert second["state"] == "final"
        assert second["turn_id"] == 0
        # 第二轮开始 turn_id 递增
        await _push(observer, LLMTextFrame(text="第二轮"))
        third = json.loads(client.call_args_list[-1].args[0])
        assert third["turn_id"] == 1

    async def test_tts_started_carries_sentence(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, TTSTextFrame(text="这句话将被播放", aggregated_by="sentence"))
        await _push(observer, TTSStartedFrame())
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "tts"
        assert payload["state"] == "started"
        assert payload["sentence"] == "这句话将被播放"

    async def test_tts_stopped_resets_chunks(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, TTSStoppedFrame())
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "tts"
        assert payload["state"] == "stopped"
        assert observer._tts_chunks == 0  # type: ignore[attr-defined]

    async def test_vad_user_speaking_frames(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, UserStartedSpeakingFrame())
        first = json.loads(client.call_args_list[0].args[0])
        assert first["type"] == "vad"
        assert first["state"] == "user_speaking"
        await _push(observer, UserStoppedSpeakingFrame())
        second = json.loads(client.call_args_list[1].args[0])
        assert second["state"] == "user_silence"

    async def test_interruption_event(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, InterruptionFrame())
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "interruption"
        assert payload["state"] == "detected"

    async def test_pipeline_started_via_callback(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await observer.on_pipeline_started()
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "system"
        assert payload["state"] == "pipeline_started"

    async def test_endframe_stops_pipeline(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, EndFrame())
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "system"
        assert payload["state"] == "pipeline_stopped"

    async def test_startframe_emits_pipeline_started(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        await _push(observer, StartFrame())
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "system"
        assert payload["state"] == "pipeline_started"


class TestDedup:
    async def test_same_frame_pushed_twice_emits_once(self) -> None:
        """同一帧经多跳传输（同 id）只序列化一次。"""
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)
        frame = LLMTextFrame(text="只发一次")
        await _push(observer, frame)
        await _push(observer, frame)
        assert client.call_count == 1


class TestTtsThrottling:
    async def test_audio_frames_throttled_in_window(self) -> None:
        """节流窗口内多个 TTS 音频帧只广播一次 synthesizing。

        chunks = 到广播时刻为止的总块数（窗口内后续帧静默，窗口外再广播）。
        """
        observer = StatusBridgeObserver(tts_throttle_secs=0.5)
        client = _upsert_mock_client(observer)
        for _ in range(5):
            await _push(observer, _tts_audio())
        assert client.call_count == 1
        payload = json.loads(client.call_args.args[0])
        assert payload["type"] == "tts"
        assert payload["state"] == "synthesizing"
        assert payload["chunks"] == 1

    async def test_audio_broadcast_again_after_window(self) -> None:
        """窗口过后再次收到音频帧 → 再次广播（chunks 累计）。"""
        observer = StatusBridgeObserver(tts_throttle_secs=0.02)
        client = _upsert_mock_client(observer)
        await _push(observer, _tts_audio())
        await asyncio.sleep(0.05)
        await _push(observer, _tts_audio())
        assert client.call_count == 2
        last = json.loads(client.call_args_list[-1].args[0])
        assert last["chunks"] == 2


class TestBroadcast:
    async def test_multicast_to_multiple_clients(self) -> None:
        observer = StatusBridgeObserver()
        c1 = AsyncMock()
        c2 = AsyncMock()
        observer.add_client(c1)
        observer.add_client(c2)
        await _push(observer, UserStartedSpeakingFrame())
        assert c1.call_count == 1
        assert c2.call_count == 1

    async def test_client_exception_does_not_block_others(self) -> None:
        """一个客户端 send 抛异常不影响其他客户端及观测循环。"""
        observer = StatusBridgeObserver()
        bad = AsyncMock(side_effect=RuntimeError("ws closed"))
        good = AsyncMock()
        observer.add_client(bad)
        observer.add_client(good)
        # on_push_frame 不应抛异常（广播层已隔离）
        await _push(observer, LLMTextFrame(text="ok"))
        await asyncio.sleep(0)
        assert good.call_count == 1
        assert bad not in observer._ws_clients  # type: ignore[attr-defined]
        assert good in observer._ws_clients  # type: ignore[attr-defined]

    async def test_no_clients_no_emit(self) -> None:
        observer = StatusBridgeObserver()
        await _push(observer, LLMTextFrame(text="无人订阅"))
        assert observer._seen_ids  # 帧仍被去重跟踪，但无广播

    async def test_slow_client_queue_remains_bounded(self) -> None:
        observer = StatusBridgeObserver(client_queue_size=2)
        gate = asyncio.Event()

        async def slow(_text: str) -> None:
            await gate.wait()

        observer.add_client(slow)
        for index in range(10):
            await observer._emit_event({"type": "test", "index": index})
        state = observer._ws_clients[slow]  # type: ignore[index]
        assert state.queue.qsize() <= 2
        assert state.dropped > 0
        gate.set()
        observer.remove_client(slow)


class TestMetrics:
    async def test_turn_metrics_emitted_on_first_tts_audio(self) -> None:
        observer = StatusBridgeObserver()
        client = _upsert_mock_client(observer)

        # 模拟一轮完整交互时序
        await _push(observer, UserStartedSpeakingFrame())
        await _push(observer, UserStoppedSpeakingFrame())
        await _push(observer, TranscriptionFrame(**_text_args(), finalized=True))
        await _push(observer, LLMTextFrame(text="你好！"))
        await _push(observer, TTSTextFrame(text="你好！", aggregated_by="sentence"))
        await _push(observer, TTSStartedFrame())
        payloads = [json.loads(c.args[0]) for c in client.call_args_list]
        assert not [p for p in payloads if p.get("type") == "metrics"]
        await _push(observer, _tts_audio())

        # 检查是否发出 type=metrics 事件
        payloads = [json.loads(c.args[0]) for c in client.call_args_list]
        metrics_events = [p for p in payloads if p.get("type") == "metrics"]
        assert len(metrics_events) == 1
        m = metrics_events[0]
        assert "turn_id" in m
        assert "stt_ms" in m
        assert "llm_ttft_ms" in m
        assert "tts_ttfb_ms" in m
        assert "e2e_ms" in m

    async def test_missing_metric_stages_are_null_and_turn_resets(self) -> None:
        observer = StatusBridgeObserver(tts_throttle_secs=0.0)
        client = _upsert_mock_client(observer)
        await _push(observer, UserStartedSpeakingFrame())
        await _push(observer, UserStoppedSpeakingFrame())
        await _push(observer, _tts_audio())
        metrics = next(
            json.loads(call.args[0])
            for call in client.call_args_list
            if json.loads(call.args[0]).get("type") == "metrics"
        )
        assert metrics["stt_ms"] is None
        assert metrics["llm_ttft_ms"] is None
        assert metrics["tts_ttfb_ms"] is None

        observer._t_stt_final = 1.0  # type: ignore[attr-defined]
        observer._t_llm_first = 2.0  # type: ignore[attr-defined]
        observer._t_tts_first = 3.0  # type: ignore[attr-defined]
        await _push(observer, UserStartedSpeakingFrame())
        assert observer._t_stt_final is None  # type: ignore[attr-defined]
        assert observer._t_llm_first is None  # type: ignore[attr-defined]
        assert observer._t_tts_first is None  # type: ignore[attr-defined]
