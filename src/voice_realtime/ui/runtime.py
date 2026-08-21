"""Voice Studio 运行时：单源采集、字幕代理与唯一交互会话。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from voice_realtime.audio.hub import AudioHub
from voice_realtime.config import Settings
from voice_realtime.interaction.nltk_data import ensure_punkt_tab
from voice_realtime.interaction.ownership import InteractionOwnership
from voice_realtime.interaction.pipeline import build_pipeline
from voice_realtime.interaction.session import InteractionSession
from voice_realtime.meeting.models import RuntimeMode, StorageHealth
from voice_realtime.meeting.runtime_mode import (
    MeetingUnavailableError,
    ModeConflictError,
    RuntimeModeCoordinator,
)
from voice_realtime.ui.assistant_bridge import StatusBridgeObserver
from voice_realtime.ui.protocol import DuplexMode, RuntimeStateSnapshot
from voice_realtime.ui.subtitle_proxy import SubtitleProxy

logger = logging.getLogger(__name__)

AUDIO_QUEUE_MAXSIZE = 256


class UIRuntime:
    """UI 进程内组件门面；交互管道生命周期统一委托给 InteractionSession。"""

    def __init__(
        self,
        settings: Settings,
        *,
        meeting_session: Any | None = None,
        mode_coordinator: RuntimeModeCoordinator | None = None,
    ) -> None:
        self._settings = settings
        self._started = False
        self._hub_active = False
        self._sinks_wired = False
        self.observer = StatusBridgeObserver()
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
        self.hub = AudioHub(device_index=settings.interaction.input_device)
        self.subtitle_proxy = SubtitleProxy(settings.subtitles)
        self.session = InteractionSession(
            settings.interaction,
            audio_queue=self.audio_queue,
            observers=[self.observer],
            ownership=InteractionOwnership(),
            pipeline_factory=build_pipeline,
        )
        self.meeting_session = meeting_session
        self.mode_coordinator = mode_coordinator
        if self.mode_coordinator is None and meeting_session is not None:
            self.mode_coordinator = RuntimeModeCoordinator(
                self.session,
                meeting_session,
                initial_mode=RuntimeMode.ASSISTANT,
            )

    def configure_meeting(self, meeting_session: Any) -> None:
        """在基础运行时启动后注入会议服务及互斥模式编排。"""
        if self.mode_coordinator is not None:
            raise RuntimeError("meeting runtime 已配置")
        self.meeting_session = meeting_session
        self.mode_coordinator = RuntimeModeCoordinator(
            self.session,
            meeting_session,
            initial_mode=(RuntimeMode.ASSISTANT if self.session.active else RuntimeMode.IDLE),
        )

    async def start(self) -> None:
        if self._started:
            return
        await self._start_subtitle_proxy()
        self._wire_sinks()
        self._hub_active = await self._start_hub()
        if self._hub_active:
            ensure_punkt_tab()
            try:
                await self.session.start()
            except Exception:
                logger.warning("UIRuntime: 交互会话启动失败", exc_info=True)
        self._started = True
        logger.info("UIRuntime: 已启动 (pipeline=%s)", self.pipelines_active)

    async def stop(self) -> None:
        if not self._started:
            return
        if self.mode_coordinator is not None:
            await self.mode_coordinator.stop()
        await self.session.stop(reason="UI 运行时停止")
        await self.hub.stop()
        await self.subtitle_proxy.stop()
        self._hub_active = False
        self._started = False
        logger.info("UIRuntime: 已停止")

    @property
    def pipelines_active(self) -> bool:
        return self.session.active

    @property
    def _worker(self) -> Any:
        """兼容既有诊断代码；实际所有权属于 InteractionSession。"""
        return self.session.worker

    @property
    def _runner_task(self) -> asyncio.Task[None] | None:
        """兼容既有诊断代码；实际所有权属于 InteractionSession。"""
        return self.session.runner_task

    @property
    def persona(self) -> str | None:
        return self.session.persona

    @property
    def duplex_mode(self) -> DuplexMode:
        return self.session.duplex_mode

    def set_persona(self, persona: str) -> None:
        self.session.set_persona(persona)

    def set_duplex_mode(self, mode: DuplexMode | str) -> None:
        self.session.set_duplex_mode(mode)

    def set_voice(self, voice: str) -> None:
        self._settings.bridge.voice = voice

    async def set_mic_muted(self, muted: bool) -> None:
        self.hub.set_muted(muted)
        if muted:
            self._drain_audio_queue()

    async def clear_context(self) -> None:
        await self.session.clear_context()

    async def stop_session(self) -> None:
        await self.session.stop(reason="用户停止会话")
        self._drain_audio_queue()

    async def restart_pipeline(self) -> None:
        if self.mode_coordinator is not None and self.mode_coordinator.mode is RuntimeMode.MEETING:
            raise ModeConflictError("会议录制期间不能重启语音管道")
        self._drain_audio_queue()
        if self._started and self._hub_active:
            await self.session.restart()

    def snapshot(self) -> RuntimeStateSnapshot:
        subtitle_state = getattr(self.subtitle_proxy, "state", "stopped")
        if hasattr(subtitle_state, "value"):
            subtitle_state = subtitle_state.value
        coordinator = self.mode_coordinator
        meeting_record = coordinator.meeting_record if coordinator is not None else None
        meeting_state = meeting_record.status if meeting_record is not None else None
        meeting_started_at = (
            meeting_record.started_at.isoformat() if meeting_record is not None else None
        )
        mode = coordinator.mode if coordinator is not None else (
            RuntimeMode.ASSISTANT if self.session.active else RuntimeMode.IDLE
        )
        return RuntimeStateSnapshot(
            pipeline=self.session.state.value,
            subtitle=str(subtitle_state),
            mic_muted=self.hub.muted,
            persona=self.session.persona,
            voice=self._settings.bridge.voice,
            duplex_mode=self.session.duplex_mode,
            session_started_at=self.session.started_at,
            mode=mode,
            active_meeting_id=(
                str(coordinator.active_meeting_id)
                if coordinator is not None and coordinator.active_meeting_id
                else None
            ),
            meeting_state=meeting_state,
            meeting_started_at=meeting_started_at,
            storage=coordinator.storage if coordinator is not None else StorageHealth.OK,
            runtime_revision=coordinator.runtime_revision if coordinator is not None else 0,
        )

    @property
    def mode(self) -> RuntimeMode:
        if self.mode_coordinator is not None:
            return self.mode_coordinator.mode
        return RuntimeMode.ASSISTANT if self.session.active else RuntimeMode.IDLE

    @property
    def active_meeting_id(self) -> Any | None:
        coordinator = self.mode_coordinator
        return coordinator.active_meeting_id if coordinator is not None else None

    @property
    def meeting_state(self) -> Any | None:
        coordinator = self.mode_coordinator
        return coordinator.meeting_state if coordinator is not None else None

    async def start_meeting(self, title: str | None = None) -> Any:
        coordinator = self.mode_coordinator
        if coordinator is None:
            raise MeetingUnavailableError("meeting service unavailable")
        return await coordinator.start_meeting(title)

    async def end_meeting(self, meeting_id: str) -> Any:
        coordinator = self.mode_coordinator
        if coordinator is None:
            raise MeetingUnavailableError("meeting service unavailable")
        return await coordinator.end_meeting(meeting_id)

    async def start_assistant(self) -> None:
        coordinator = self.mode_coordinator
        if coordinator is None:
            if not self.session.active and self._started and self._hub_active:
                await self.session.start()
            return
        await coordinator.start_assistant()

    async def stop_active_mode(self) -> None:
        coordinator = self.mode_coordinator
        if coordinator is None:
            await self.session.stop(reason="停止当前模式")
            return
        await coordinator.stop_active_mode()

    async def _start_subtitle_proxy(self) -> None:
        try:
            await self.subtitle_proxy.start()
        except Exception:
            logger.warning("UIRuntime: SubtitleProxy 启动失败（wlk 未运行？）", exc_info=True)

    def _wire_sinks(self) -> None:
        if self._sinks_wired:
            return
        self.hub.add_sink("pipecat", self._enqueue_audio)
        self.hub.add_sink("subtitle", self.subtitle_proxy.push_audio)
        self._sinks_wired = True

    async def _start_hub(self) -> bool:
        try:
            await self.hub.start()
            return True
        except Exception:
            logger.warning("UIRuntime: AudioHub 启动失败（麦克风不可用？）", exc_info=True)
            return False

    async def _enqueue_audio(self, data: bytes) -> None:
        if self.hub.muted or (
            self.mode_coordinator is not None
            and self.mode_coordinator.mode is RuntimeMode.MEETING
        ):
            return
        with contextlib.suppress(asyncio.QueueFull):
            self.audio_queue.put_nowait(data)

    def _drain_audio_queue(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.task_done()
            except asyncio.QueueEmpty:
                return
