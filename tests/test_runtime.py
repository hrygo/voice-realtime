"""UIRuntime 装配单测：生命周期 + 容错 + 背压（组件全部 mock）。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_realtime.config import Settings
from voice_realtime.ui.runtime import AUDIO_QUEUE_MAXSIZE, UIRuntime


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
    return tuple(
        stack.enter_context(patch(path))
        for path in (
            "voice_realtime.ui.runtime.SubtitleProxy",
            "voice_realtime.ui.runtime.AudioHub",
            "voice_realtime.ui.runtime.build_pipeline",
            "voice_realtime.interaction.session.PipelineWorker",
            "voice_realtime.interaction.session.WorkerRunner",
        )
    )


async def _hung() -> None:
    """永久挂起（模拟持续运行的管道 worker）。"""
    await asyncio.Event().wait()


def _mock_async_components(proxy, hub, runner) -> None:
    """把关键方法升级为 async mock；runner.end 触发 run 优雅退出。"""
    stopped = asyncio.Event()

    async def end(*_args, **_kwargs) -> None:
        stopped.set()

    proxy.start = AsyncMock()
    proxy.stop = AsyncMock()
    proxy.push_audio = AsyncMock()
    hub.start = AsyncMock()
    hub.stop = AsyncMock()
    hub.add_sink = MagicMock()
    hub.muted = False
    hub.set_muted = MagicMock()
    runner.add_workers = AsyncMock()
    runner.run = AsyncMock(side_effect=stopped.wait)
    runner.end = AsyncMock(side_effect=end)


class TestStart:
    async def test_start_assembles_all_components(self, settings: Settings) -> None:
        """start 应依次：启动字幕代理 → 接两个 sink → 开麦 → 装配并运行管道。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await asyncio.sleep(0)  # 让 runner task 进入挂起

        proxy.start.assert_awaited_once()
        hub.add_sink.assert_any_call("pipecat", runtime._enqueue_audio)
        hub.add_sink.assert_any_call("subtitle", runtime._push_subtitle_audio)
        hub.start.assert_awaited_once()
        build.assert_called_once()
        _, kwargs = worker_cls.call_args
        assert kwargs["observers"] == [runtime.observer]

    async def test_push_subtitle_audio_echo_gating(self, settings: Settings) -> None:
        """测试在助手模式下 TTS 播报期间阻断音频流向字幕代理，非播报期间放行。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            build.return_value = MagicMock(name="pipeline")

            await runtime.start()
            await asyncio.sleep(0)

            # 初始状态：未播报，音频正常放行给字幕代理
            await runtime._push_subtitle_audio(b"\x01\x02" * 256)
            proxy.push_audio.assert_awaited_once_with(b"\x01\x02" * 256)
            proxy.push_audio.reset_mock()

            # 激活 TTS 播报：此时为外放回声，应阻断推流
            runtime.session.echo_state.on_tts_started()
            assert runtime.session.is_echo_suppressing()
            await runtime._push_subtitle_audio(b"\x01\x02" * 256)
            proxy.push_audio.assert_not_awaited()

            # 静音状态下也不推流
            runtime.session.echo_state.reset()
            hub.muted = True
            await runtime._push_subtitle_audio(b"\x01\x02" * 256)
            proxy.push_audio.assert_not_awaited()

    async def test_hub_failure_skips_pipeline(self, settings: Settings) -> None:
        """麦克风不可用时管道不装配，但 runtime 仍视为已启动（其余能力可用）。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            hub.start = AsyncMock(side_effect=OSError("no mic"))

            await runtime.start()

        build.assert_not_called()
        assert not runtime.pipelines_active
        await runtime.stop()  # 停止路径不抛错


class TestSubtitleProxyFailure:
    async def test_proxy_failure_nonfatal(self, settings: Settings) -> None:
        """wlk 不在线时 SubtitleProxy.start 抛错不阻断其余启动。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, _build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
            hub = hub_cls.return_value
            runner = runner_cls.return_value
            _mock_async_components(proxy, hub, runner)
            proxy.start = AsyncMock(side_effect=ConnectionRefusedError("wlk down"))

            await runtime.start()  # 不应抛
            assert runtime._started
            await runtime.stop()

        # 字幕代理挂掉：采集/管道仍继续（麦克风扇出不受影响）
        hub.add_sink.assert_any_call("pipecat", runtime._enqueue_audio)


class TestBackpressure:
    async def test_enqueue_drops_when_queue_full(self, settings: Settings) -> None:
        """队满时 put_nowait 静默丢帧（有界背压不阻塞采集）。"""
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            for _ in range(AUDIO_QUEUE_MAXSIZE):
                runtime.audio_queue.put_nowait(b"\x00" * 512)
            assert runtime.audio_queue.full()
            await runtime._enqueue_audio(b"\x01" * 512)  # 不抛
            assert runtime.audio_queue.qsize() == AUDIO_QUEUE_MAXSIZE


class TestStop:
    async def test_stop_cancels_pipeline_and_closes_components(
        self, settings: Settings
    ) -> None:
        """stop 逆序清理：取消 runner task，关闭 hub 与 proxy。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
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

            await runtime.stop()

        assert runner_task.done()
        runner.end.assert_awaited_once()
        hub.stop.assert_awaited_once()
        proxy.stop.assert_awaited_once()
        assert not runtime.pipelines_active

    async def test_double_start_is_noop(self, settings: Settings) -> None:
        """重复 start 幂等（不重复开麦/起管道）。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
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
            proxy_cls, hub_cls, build, worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
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

    async def test_stop_session_stops_pipeline_only(
        self, settings: Settings
    ) -> None:
        """stop_session 停管道（runner task cancelled），保留 hub/proxy。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _worker_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
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
        runner.end.assert_awaited_once()
        hub.start.assert_awaited_once()
        proxy.start.assert_awaited_once()
        # stop_session 不关闭 hub/proxy
        hub.stop.assert_not_awaited()
        proxy.stop.assert_not_awaited()

    async def test_restart_pipeline_rebuilds(self, settings: Settings) -> None:
        """restart_pipeline 停止旧管道后重新装配。"""
        with ExitStack() as stack:
            proxy_cls, hub_cls, build, _w_cls, runner_cls = _patched(stack)
            runtime = UIRuntime(settings)
            proxy = proxy_cls.return_value
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

    def test_configure_meeting_installs_runtime_coordinator(self, settings: Settings) -> None:
        with ExitStack() as stack:
            _patched(stack)
            runtime = UIRuntime(settings)
            meeting = MagicMock()

            runtime.configure_meeting(meeting)

        assert runtime.meeting_session is meeting
        assert runtime.mode_coordinator is not None
        assert runtime.mode_coordinator.meeting is meeting
