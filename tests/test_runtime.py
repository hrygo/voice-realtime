"""UIRuntime 装配单测：生命周期 + 容错 + 背压（组件全部 mock）。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from voice_realtime.config import Settings
from voice_realtime.meeting.models import PCMOwner, RuntimeMode
from voice_realtime.meeting.runtime_mode import (
    MeetingUnavailableError,
    ModeConflictError,
    RuntimeModeCoordinator,
)
from voice_realtime.ui.runtime import AUDIO_QUEUE_MAXSIZE, UIRuntime


class _FakeSubtitleProxy:
    """声明完整 workload 接口，同时保留可断言的 mock 行为。"""

    def __init__(self, *_args, **_kwargs) -> None:
        self.state = "paused"
        self._browser_capture_active = False
        self.accepted_audio: list[bytes] = []
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.push_audio = AsyncMock(side_effect=self._push_audio)
        self.prepare_browser_capture = AsyncMock(side_effect=self._prepare_browser_capture)
        self.commit_browser_capture = MagicMock(side_effect=self._commit_browser_capture)
        self.abort_browser_capture = AsyncMock()
        self.deactivate_browser_capture = AsyncMock(
            side_effect=self._deactivate_browser_capture
        )
        self.diagnostic_snapshot = MagicMock(name="subtitle_diagnostics")
        self.diagnostics = MagicMock(return_value=self.diagnostic_snapshot)

    @property
    def browser_capture_active(self) -> bool:
        return self._browser_capture_active

    async def _push_audio(self, data: bytes) -> None:
        self.accepted_audio.append(data)

    async def _prepare_browser_capture(self, *, timeout_secs: float) -> object:
        del timeout_secs
        return object()

    def _commit_browser_capture(self, _preparation: object) -> None:
        self._browser_capture_active = True

    async def _deactivate_browser_capture(self) -> None:
        self._browser_capture_active = False

    async def prepare_browser_capture(self, *, timeout_secs: float) -> object:
        raise AssertionError(timeout_secs)

    def commit_browser_capture(self, preparation: object) -> None:
        raise AssertionError(preparation)

    async def abort_browser_capture(self, preparation: object) -> None:
        raise AssertionError(preparation)

    async def deactivate_browser_capture(self) -> None:
        raise AssertionError

    async def push_audio(self, data: bytes) -> None:
        raise AssertionError(data)

    def diagnostics(self, expected_owner: PCMOwner) -> object:
        raise AssertionError(expected_owner)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        bridge={"host": "127.0.0.1", "port": 9999},
        subtitles={"host": "127.0.0.1", "port": 9998},
        interaction={"llm_base_url": "http://127.0.0.1:9997/v1"},
    )


def _patched(stack: ExitStack) -> tuple:
    """在 stack 上 patch 外部依赖，返回 mock 类元组。

    用法：
        with ExitStack() as stack:
            proxy_cls, hub_cls, *_ = _patched(stack)
            runtime = UIRuntime(settings)   # 必须在 with 内构造（__init__ 即 new 组件）
    """
    stack.enter_context(patch("voice_realtime.ui.runtime.InteractionOwnership"))
    stack.enter_context(patch("voice_realtime.ui.runtime.ensure_punkt_tab", return_value=True))
    proxy_cls = stack.enter_context(
        patch("voice_realtime.ui.runtime.SubtitleProxy", side_effect=_FakeSubtitleProxy)
    )
    others = tuple(
        stack.enter_context(patch(path))
        for path in (
            "voice_realtime.ui.runtime.AudioHub",
            "voice_realtime.ui.runtime.build_pipeline",
            "voice_realtime.interaction.session.PipelineWorker",
            "voice_realtime.interaction.session.WorkerRunner",
        )
    )
    return (proxy_cls, *others)


async def _hung() -> None:
    """永久挂起（模拟持续运行的管道 worker）。"""
    await asyncio.Event().wait()


def _mock_async_components(_proxy, hub, runner) -> None:
    """把关键方法升级为 async mock；runner.end 触发 run 优雅退出。"""
    stopped = asyncio.Event()

    async def end(*_args, **_kwargs) -> None:
        stopped.set()

    hub.start = AsyncMock()
    hub.stop = AsyncMock()
    hub.add_sink = MagicMock()
    hub.muted = False
    hub.set_muted = MagicMock()
    runner.add_workers = AsyncMock()
    runner.run = AsyncMock(side_effect=stopped.wait)
    runner.end = AsyncMock(side_effect=end)


def test_runtime_passes_asr_registry_to_subtitle_proxy(settings: Settings) -> None:
    registry = MagicMock(name="asr_registry")
    with ExitStack() as stack:
        proxy_cls, *_ = _patched(stack)

        UIRuntime(settings, asr_registry=registry)

    proxy_cls.assert_called_once_with(settings.subtitles, registry=registry)


def test_runtime_passes_conversation_stt_factory_to_session(settings: Settings) -> None:
    stt_factory = MagicMock(name="conversation_stt_factory")
    with ExitStack() as stack:
        _patched(stack)

        runtime = UIRuntime(settings, conversation_stt_factory=stt_factory)

    assert runtime.session._stt_factory is stt_factory  # type: ignore[attr-defined]


def test_runtime_constructs_one_idle_coordinator_and_broadcaster(
    settings: Settings,
) -> None:
    with ExitStack() as stack:
        _patched(stack)
        runtime = UIRuntime(settings)

    assert runtime.mode_coordinator.mode is RuntimeMode.IDLE
    assert runtime.mode_coordinator.pcm_owner is PCMOwner.NONE
    assert runtime.snapshot().runtime_revision == 0
    assert runtime.runtime_events is not None
    with pytest.raises(AttributeError):
        runtime.mode_coordinator = MagicMock()


class TestStart:
    async def test_start_assembles_all_components(self, settings: Settings) -> None:
        """start 应依次：启动字幕代理 → 接两个 sink → 开麦 → 装配并运行管道。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await asyncio.sleep(0)  # 让 runner task 进入挂起

        proxy.start.assert_awaited_once()
        proxy.prepare_browser_capture.assert_not_awaited()
        hub.add_sink.assert_any_call("pipecat", runtime._enqueue_audio)
        hub.add_sink.assert_any_call("subtitle", runtime._push_subtitle_audio)
        hub.start.assert_awaited_once()
        build.assert_called_once()
        _, kwargs = worker_cls.call_args
        assert kwargs["observers"] == [runtime.observer]
        assert runtime.snapshot().mode is RuntimeMode.ASSISTANT
        assert runtime.snapshot().pcm_owner is PCMOwner.ASSISTANT
        transition = runtime.diagnostics()["last_transition"]
        assert transition is not None
        assert transition["target"] == "assistant"
        assert transition["result"] == "success"

    async def test_push_subtitle_audio_keeps_mic_and_echo_gates(
        self, settings: Settings
    ) -> None:
        """owner 放行时仍优先执行静音与 TTS 回声门控。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, _build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            runtime._set_pcm_owner(PCMOwner.SUBTITLES)
            with patch.object(
                RuntimeModeCoordinator,
                "mode",
                new_callable=PropertyMock,
                return_value=RuntimeMode.ASSISTANT,
            ), patch.object(
                type(runtime.session),
                "active",
                new_callable=PropertyMock,
                return_value=True,
            ):
                runtime.session.is_echo_suppressing = MagicMock(return_value=True)
                await runtime._push_subtitle_audio(b"\x01\x02" * 256)
            proxy.push_audio.assert_not_awaited()

            runtime.session.is_echo_suppressing = MagicMock(return_value=False)
            hub.muted = True
            await runtime._push_subtitle_audio(b"\x01\x02" * 256)
            proxy.push_audio.assert_not_awaited()

    async def test_hub_failure_skips_pipeline(self, settings: Settings) -> None:
        """麦克风不可用时管道不装配，但 runtime 仍视为已启动（其余能力可用）。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            hub.start = AsyncMock(side_effect=OSError("no mic"))

            await runtime.start()

        build.assert_not_called()
        assert not runtime.pipelines_active
        assert runtime.snapshot().mode is RuntimeMode.IDLE
        assert runtime.snapshot().pcm_owner is PCMOwner.NONE
        await runtime.stop()  # 停止路径不抛错

    async def test_interaction_start_failure_stays_idle_and_stoppable(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _proxy_cls, hub_cls, _build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            runtime.session.start = AsyncMock(side_effect=RuntimeError("start failed"))

            await runtime.start()

            assert runtime._started
            assert runtime.snapshot().mode is RuntimeMode.IDLE
            assert runtime.snapshot().pcm_owner is PCMOwner.NONE
            await runtime.stop()

        hub.stop.assert_awaited_once()
        proxy.stop.assert_awaited_once()


class TestSubtitleProxyFailure:
    async def test_proxy_failure_nonfatal(self, settings: Settings) -> None:
        """wlk 不在线时 SubtitleProxy.start 抛错不阻断其余启动。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, _build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            proxy.start = AsyncMock(side_effect=ConnectionRefusedError("wlk down"))

            await runtime.start()  # 不应抛
            assert runtime._started
            assert runtime.snapshot().mode is RuntimeMode.ASSISTANT
            assert runtime.snapshot().pcm_owner is PCMOwner.ASSISTANT
            proxy.prepare_browser_capture.assert_not_awaited()
            await runtime.stop()

        # 字幕代理挂掉：采集/管道仍继续（麦克风扇出不受影响）
        hub.add_sink.assert_any_call("pipecat", runtime._enqueue_audio)


class TestPCMOwnership:
    @pytest.mark.parametrize(
        ("owner", "interaction_chunks", "subtitle_chunks"),
        [
            (PCMOwner.ASSISTANT, 1, 0),
            (PCMOwner.SUBTITLES, 0, 1),
            (PCMOwner.MEETING, 0, 1),
            (PCMOwner.NONE, 0, 0),
        ],
    )
    async def test_pcm_gate_matrix(
        self,
        settings: Settings,
        owner: PCMOwner,
        interaction_chunks: int,
        subtitle_chunks: int,
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime.hub.muted = False

            runtime._set_pcm_owner(owner)
            with patch.object(
                type(runtime.session),
                "active",
                new_callable=PropertyMock,
                return_value=owner is PCMOwner.ASSISTANT,
            ):
                await runtime._enqueue_audio(b"pcm")
            await runtime._push_subtitle_audio(b"pcm")

        assert runtime.audio_queue.qsize() == interaction_chunks
        assert runtime.subtitle_proxy.push_audio.await_count == subtitle_chunks

    async def test_assistant_owner_rejects_audio_when_interaction_inactive(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime.hub.muted = False
            runtime._set_pcm_owner(PCMOwner.ASSISTANT)

            assert not runtime.session.active
            await runtime._enqueue_audio(b"orphaned")

        interaction = runtime.diagnostics()["interaction"]
        assert interaction == {"queued_chunks": 0, "dropped_chunks": 0}

    async def test_none_drains_only_interaction_queue_and_preserves_meeting_audio(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime.hub.muted = False
            runtime._set_pcm_owner(PCMOwner.ASSISTANT)
            await runtime._enqueue_audio(b"interaction")
            runtime._set_pcm_owner(PCMOwner.MEETING)
            await runtime._push_subtitle_audio(b"meeting-accepted")

            runtime._set_pcm_owner(PCMOwner.NONE)
            await runtime._enqueue_audio(b"new-interaction")
            await runtime._push_subtitle_audio(b"new-subtitle")

        assert runtime.audio_queue.empty()
        assert runtime.subtitle_proxy.accepted_audio == [b"meeting-accepted"]
        runtime.subtitle_proxy.deactivate_browser_capture.assert_not_awaited()

    async def test_prepared_subtitle_target_receives_no_pcm(
        self, settings: Settings
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def prepare(*, timeout_secs: float) -> object:
            del timeout_secs
            entered.set()
            await release.wait()
            return object()

        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            await runtime.start()
            await asyncio.sleep(0)
            proxy.prepare_browser_capture.side_effect = prepare

            transition = asyncio.create_task(runtime.start_subtitles())
            await entered.wait()
            await runtime._push_subtitle_audio(b"prepared")

            proxy.push_audio.assert_not_awaited()
            assert runtime.snapshot().pcm_owner is PCMOwner.ASSISTANT
            release.set()
            await transition
            await runtime.stop()


class TestBackpressure:
    async def test_enqueue_drops_when_queue_full(self, settings: Settings) -> None:
        """队满时 put_nowait 静默丢帧（有界背压不阻塞采集）。"""
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime._set_pcm_owner(PCMOwner.ASSISTANT)
            for _ in range(AUDIO_QUEUE_MAXSIZE):
                runtime.audio_queue.put_nowait(b"\x00" * 512)
            assert runtime.audio_queue.full()
            with patch.object(
                type(runtime.session),
                "active",
                new_callable=PropertyMock,
                return_value=True,
            ):
                await runtime._enqueue_audio(b"\x01" * 512)  # 不抛
            assert runtime.audio_queue.qsize() == AUDIO_QUEUE_MAXSIZE

    async def test_enqueue_rejects_new_chunk_and_reports_exact_drop_count(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime.hub.muted = False
            runtime._set_pcm_owner(PCMOwner.ASSISTANT)
            runtime.audio_queue = asyncio.Queue(maxsize=1)
            runtime.audio_queue.put_nowait(b"queued")

            with patch.object(
                type(runtime.session),
                "active",
                new_callable=PropertyMock,
                return_value=True,
            ):
                await runtime._enqueue_audio(b"rejected")

            with patch.object(
                RuntimeModeCoordinator,
                "pcm_owner",
                new_callable=PropertyMock,
                return_value=PCMOwner.ASSISTANT,
            ):
                diagnostics = runtime.diagnostics()
            assert diagnostics["interaction"] == {
                "queued_chunks": 1,
                "dropped_chunks": 1,
            }
            assert diagnostics["last_transition"] is None
            assert diagnostics["subtitles"] is runtime.subtitle_proxy.diagnostic_snapshot
            runtime.subtitle_proxy.diagnostics.assert_called_with(PCMOwner.ASSISTANT)
            assert runtime.diagnostics()["interaction"]["dropped_chunks"] == 1
            assert "queue" not in diagnostics["interaction"]
            assert "bytes" not in diagnostics["interaction"]

            assert runtime.audio_queue.get_nowait() == b"queued"
            runtime.audio_queue.task_done()


class TestRuntimeStateBroadcasts:
    async def test_successful_transition_broadcasts_same_full_snapshot_to_clients(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            first = runtime.runtime_events.add_client()
            second = runtime.runtime_events.add_client()
            assert first.latest_nowait().mode is RuntimeMode.IDLE
            assert second.latest_nowait().pcm_owner is PCMOwner.NONE

            await runtime.start()
            await asyncio.sleep(0)
            first_state = await first.receive()
            second_state = await second.receive()

            assert first_state.model_dump() == second_state.model_dump()
            assert first_state.model_dump() == runtime.snapshot().model_dump()
            assert first_state.mode is RuntimeMode.ASSISTANT
            assert first_state.pcm_owner is PCMOwner.ASSISTANT
            assert first_state.runtime_revision == 1
            await runtime.stop()

    async def test_target_prepare_failure_does_not_broadcast(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            await runtime.start()
            await asyncio.sleep(0)
            client = runtime.runtime_events.add_client()
            before = client.latest_nowait()
            proxy.prepare_browser_capture.side_effect = OSError("wlk unavailable")

            with pytest.raises(OSError, match="wlk unavailable"):
                await runtime.start_subtitles()

            assert runtime.snapshot().runtime_revision == before.runtime_revision
            with pytest.raises(asyncio.QueueEmpty):
                client.latest_nowait()
            await runtime.stop()

    async def test_source_compensation_broadcasts_restored_snapshot_once(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            await runtime.start()
            await asyncio.sleep(0)
            client = runtime.runtime_events.add_client()
            before = client.latest_nowait()
            with patch.object(
                runtime.session,
                "stop",
                new=AsyncMock(side_effect=OSError("quiesce failed")),
            ), pytest.raises(OSError, match="quiesce failed"):
                await runtime.start_subtitles()

            restored = await client.receive()
            assert restored.mode is RuntimeMode.ASSISTANT
            assert restored.pcm_owner is PCMOwner.ASSISTANT
            assert restored.runtime_revision == before.runtime_revision + 1
            with pytest.raises(asyncio.QueueEmpty):
                client.latest_nowait()
            proxy.abort_browser_capture.assert_awaited_once()
            await runtime.stop()

    async def test_failed_compensation_broadcasts_forced_idle_once(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            await runtime.start()
            await asyncio.sleep(0)
            client = runtime.runtime_events.add_client()
            before = client.latest_nowait()
            original_stop = runtime.session.stop

            async def stop_then_fail(*, reason: str) -> None:
                await original_stop(reason=reason)
                raise OSError("quiesce failed")

            runtime.session.stop = AsyncMock(side_effect=stop_then_fail)
            runtime.session.start = AsyncMock(side_effect=OSError("restore failed"))

            with pytest.raises(MeetingUnavailableError, match="运行时工作负载恢复失败"):
                await runtime.start_subtitles()

            forced_idle = await client.receive()
            assert forced_idle.mode is RuntimeMode.IDLE
            assert forced_idle.pcm_owner is PCMOwner.NONE
            assert forced_idle.runtime_revision == before.runtime_revision + 1
            with pytest.raises(asyncio.QueueEmpty):
                client.latest_nowait()
            await runtime.stop()


class TestStop:
    async def test_stop_cancels_pipeline_and_closes_components(
        self, settings: Settings
    ) -> None:
        """stop 逆序清理：取消 runner task，关闭 hub 与 proxy。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            proxy.stop = AsyncMock()
            hub.stop = AsyncMock()
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await asyncio.sleep(0)
            runner_task = runtime._runner_task
            assert runner_task is not None
            client = runtime.runtime_events.add_client()
            assistant_state = client.latest_nowait()

            await runtime.stop()
            shutdown_state = await client.receive()

        assert runner_task.done()
        runner.end.assert_awaited_once()
        hub.stop.assert_awaited_once()
        proxy.stop.assert_awaited_once()
        assert not runtime.pipelines_active
        assert shutdown_state.mode is RuntimeMode.IDLE
        assert shutdown_state.pcm_owner is PCMOwner.NONE
        assert shutdown_state.runtime_revision == assistant_state.runtime_revision + 1

    async def test_stop_orders_coordinator_before_hub_and_proxy(
        self, settings: Settings
    ) -> None:
        order: list[str] = []
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            await runtime.start()
            original_stop = runtime.mode_coordinator.stop

            async def stop_coordinator() -> None:
                order.append("coordinator")
                await original_stop()

            runtime.mode_coordinator.stop = AsyncMock(side_effect=stop_coordinator)
            hub.stop.side_effect = lambda: order.append("hub")
            proxy.stop.side_effect = lambda: order.append("proxy")

            await runtime.stop()

        assert order == ["coordinator", "hub", "proxy"]

    async def test_double_start_is_noop(self, settings: Settings) -> None:
        """重复 start 幂等（不重复开麦/起管道）。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await runtime.start()

        assert hub.start.await_count == 1
        assert build.call_count == 1


class TestControlCommands:
    async def test_clear_context_updates_llm_context(
        self, settings: Settings
    ) -> None:
        """clear_context 经 worker.queue_frame 推 LLMMessagesUpdateFrame(sys prompt)。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")
            worker = worker_cls.return_value
            worker.queue_frame = AsyncMock()

            await runtime.start()
            await asyncio.sleep(0)
            runtime.set_persona("你是孔子")
            await runtime.clear_context()

        assert worker.queue_frame.await_count >= 1
        frame = worker.queue_frame.await_args.args[0]
        from pipecat.frames.frames import LLMMessagesUpdateFrame

        assert isinstance(frame, LLMMessagesUpdateFrame)
        assert len(frame.messages) == 1
        assert frame.messages[0]["role"] == "system"
        assert "孔子" in str(frame.messages[0]["content"])

    async def test_clear_context_noop_without_worker(self, settings: Settings) -> None:
        """管道未装配时 clear_context 静默无操作。"""
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            await runtime.clear_context()  # 不抛

    async def test_stop_session_stops_active_mode_and_keeps_services(
        self, settings: Settings
    ) -> None:
        """stop_session 经 coordinator 收敛 idle/none，并保留 hub/proxy。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await asyncio.sleep(0)
            task = runtime._runner_task
            assert task is not None
            await runtime.stop_session()

        assert task.done()
        assert runtime.snapshot().mode is RuntimeMode.IDLE
        assert runtime.snapshot().pcm_owner is PCMOwner.NONE
        runner.end.assert_awaited_once()
        hub.start.assert_awaited_once()
        proxy.start.assert_awaited_once()
        # stop_session 不关闭 hub/proxy
        hub.stop.assert_not_awaited()
        proxy.stop.assert_not_awaited()

    async def test_restart_pipeline_rebuilds(self, settings: Settings) -> None:
        """restart_pipeline 停止旧管道后重新装配。"""
        with ExitStack() as stack:
            _proxy_cls, hub_cls, build, _w_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = runtime.subtitle_proxy
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)

            await runtime.start()
            await asyncio.sleep(0)
            first_task = runtime._runner_task
            assert first_task is not None

            await runtime.restart_pipeline()
            await asyncio.sleep(0)

        assert first_task.done()
        assert build.call_count == 2

    @pytest.mark.parametrize(
        "mode",
        [RuntimeMode.IDLE, RuntimeMode.SUBTITLES, RuntimeMode.MEETING],
    )
    async def test_restart_pipeline_rejects_non_assistant_modes(
        self, settings: Settings, mode: RuntimeMode
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime._started = True
            runtime._hub_active = True
            runtime.session.restart = AsyncMock()
            with patch.object(
                RuntimeModeCoordinator,
                "mode",
                new_callable=PropertyMock,
                return_value=mode,
            ), pytest.raises(ModeConflictError) as exc_info:
                await runtime.restart_pipeline()

        assert exc_info.value.code == "mode_conflict"
        runtime.session.restart.assert_not_awaited()

    async def test_restart_pipeline_rejects_assistant_transition_barrier(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            runtime._started = True
            runtime._hub_active = True
            runtime.session.restart = AsyncMock()
            with patch.object(
                RuntimeModeCoordinator,
                "mode",
                new_callable=PropertyMock,
                return_value=RuntimeMode.ASSISTANT,
            ), patch.object(
                RuntimeModeCoordinator,
                "pcm_owner",
                new_callable=PropertyMock,
                return_value=PCMOwner.NONE,
            ), pytest.raises(ModeConflictError):
                await runtime.restart_pipeline()

        runtime.session.restart.assert_not_awaited()

    def test_configure_meeting_keeps_runtime_coordinator(self, settings: Settings) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            meeting = MagicMock()
            coordinator = runtime.mode_coordinator
            broadcaster = runtime.runtime_events

            runtime.configure_meeting(meeting)

        assert runtime.meeting_session is meeting
        assert runtime.mode_coordinator is coordinator
        assert runtime.runtime_events is broadcaster
        assert runtime.mode_coordinator.meeting is meeting

    async def test_start_subtitles_delegates_to_coordinator(
        self, settings: Settings
    ) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)

            await runtime.start_subtitles()

        assert runtime.snapshot().mode is RuntimeMode.SUBTITLES
        assert runtime.snapshot().pcm_owner is PCMOwner.SUBTITLES
