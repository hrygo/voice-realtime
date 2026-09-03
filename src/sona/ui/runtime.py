"""Sona 运行时：单源采集、字幕代理与唯一交互会话。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import httpx

from sona.asr.contracts import ConversationSTTFactory
from sona.audio.frame import AudioSourceKind
from sona.audio.hub import AudioHub
from sona.audio.levels import AudioLevelMeter
from sona.config import Settings, normalize_speechrail_tts_voice
from sona.interaction.nltk_data import ensure_punkt_tab
from sona.interaction.ownership import InteractionOwnership
from sona.interaction.pipeline import build_pipeline
from sona.interaction.pipeline_dependencies import default_pipeline_factories
from sona.interaction.session import InteractionSession
from sona.meeting.models import PCMOwner, RuntimeMode
from sona.meeting.runtime_mode import (
    ModeConflictError,
    RuntimeModeCoordinator,
)
from sona.subtitles.proxy import SubtitleProxy
from sona.ui.assistant_bridge import StatusBridgeObserver
from sona.ui.protocol import (
    AudioLevelsSnapshot,
    DuplexMode,
    RuntimeCapabilities,
    RuntimeStateSnapshot,
)
from sona.ui.runtime_events import RuntimeStateBroadcaster

logger = logging.getLogger(__name__)

AUDIO_QUEUE_MAXSIZE = 256


def _speechrail_readiness_probe(
    settings: Settings, *, timeout_secs: float = 0.5
) -> Any:
    """基于 SpeechRail /health 的就绪探针，worker 重启期间不触发 WS 盲连。"""

    health_url = settings.subtitles.speechrail_health_url
    probe_timeout = timeout_secs

    async def probe() -> bool:
        try:
            async with httpx.AsyncClient(timeout=probe_timeout) as client:
                response = await client.get(health_url)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return bool(payload.get("ready") or payload.get("asr_ready"))

    return probe


class UIRuntime:
    """UI 进程内组件门面；交互管道生命周期统一委托给 InteractionSession。"""

    def __init__(
        self,
        settings: Settings,
        *,
        meeting_session: Any | None = None,
        conversation_stt_factory: ConversationSTTFactory | None = None,
    ) -> None:
        self._settings = settings
        self._started = False
        self._hub_active = False
        self._sinks_wired = False
        self.observer = StatusBridgeObserver()
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
        self._interaction_dropped_chunks = 0
        self._pcm_owner = PCMOwner.NONE
        self._audio_levels = AudioLevelMeter()
        self.hub = AudioHub(
            device_index=settings.interaction.input_device,
            device_name=settings.interaction.input_device_name,
        )
        self.subtitle_proxy = SubtitleProxy(
            settings.subtitles,
            readiness_probe=_speechrail_readiness_probe(settings),
        )
        factories = default_pipeline_factories(settings.interaction)
        if conversation_stt_factory is not None:
            factories = dataclasses.replace(factories, stt_factory=conversation_stt_factory)
        self.session = InteractionSession(
            settings.interaction,
            audio_queue=self.audio_queue,
            observers=[self.observer],
            ownership=InteractionOwnership(),
            pipeline_factory=build_pipeline,
            pipeline_factories=factories,
        )
        self.meeting_session = meeting_session
        self._coordinator = RuntimeModeCoordinator(
            self.session,
            self.subtitle_proxy,
            meeting_session=meeting_session,
            initial_mode=RuntimeMode.IDLE,
            on_owner_changed=self._set_pcm_owner,
            state_publisher=self._publish_runtime_state,
        )
        self.runtime_events = RuntimeStateBroadcaster(self.snapshot)

    def configure_meeting(self, meeting_session: Any) -> None:
        """在基础运行时启动后注入会议服务及互斥模式编排。"""
        self._coordinator.configure_meeting(meeting_session)
        self.meeting_session = meeting_session

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._start_subtitle_proxy()
        self._wire_sinks()
        self._hub_active = await self._start_hub()
        if self._hub_active:
            try:
                ensure_punkt_tab()
                await self._coordinator.start_assistant()
            except Exception:
                logger.warning("UIRuntime: 交互会话启动失败", exc_info=True)
        logger.info("UIRuntime: 已启动 (pipeline=%s)", self.pipelines_active)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await self._coordinator.stop()
        finally:
            try:
                await self.hub.stop()
            finally:
                try:
                    await self.subtitle_proxy.stop()
                finally:
                    self._hub_active = False
                    self._started = False
        logger.info("UIRuntime: 已停止")

    @property
    def mode_coordinator(self) -> RuntimeModeCoordinator:
        """返回构造时建立且生命周期内不可替换的模式协调器。"""
        return self._coordinator

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

    async def set_voice(self, voice: str) -> None:
        normalized = normalize_speechrail_tts_voice(voice)
        await self.session.set_voice(normalized)

    async def set_mic_muted(self, muted: bool) -> None:
        self.hub.set_muted(muted)
        if muted:
            self._drain_audio_queue()
            self._audio_levels.clear(AudioSourceKind.MICROPHONE)
            self._publish_runtime_state()

    async def clear_context(self) -> None:
        await self.session.clear_context()

    async def send_text(self, text: str) -> None:
        if self._coordinator.mode is RuntimeMode.MEETING:
            raise ModeConflictError("会议录制期间不能向语音助手发送文本")
        if not self.session.active:
            raise RuntimeError("语音助手会话未在运行中")
        await self.session.send_text(text)

    async def clear_subtitles(self) -> None:
        await self.subtitle_proxy.clear_subtitles()

    async def stop_session(self) -> None:
        await self._coordinator.stop_active_mode()

    async def restart_pipeline(self) -> None:
        async def restart() -> None:
            self._drain_audio_queue()
            if self._started and self._hub_active:
                await self.session.restart()

        await self._coordinator.restart_assistant(restart)

    def snapshot(self) -> RuntimeStateSnapshot:
        subtitle_state = getattr(self.subtitle_proxy, "state", "stopped")
        if hasattr(subtitle_state, "value"):
            subtitle_state = subtitle_state.value
        coordinator = self._coordinator
        meeting_record = coordinator.meeting_record
        meeting_state = meeting_record.status if meeting_record is not None else None
        meeting_started_at = (
            meeting_record.started_at.isoformat() if meeting_record is not None else None
        )
        audio_levels = self._audio_levels.snapshot()
        return RuntimeStateSnapshot(
            pipeline=self.session.state.value,
            subtitle=str(subtitle_state),
            mic_muted=self.hub.muted,
            persona=self.session.persona,
            voice=self._settings.interaction.tts_voice,
            duplex_mode=self.session.duplex_mode,
            session_started_at=self.session.started_at,
            mode=coordinator.mode,
            pcm_owner=coordinator.pcm_owner,
            active_meeting_id=(
                str(coordinator.active_meeting_id)
                if coordinator.active_meeting_id
                else None
            ),
            meeting_state=meeting_state,
            meeting_started_at=meeting_started_at,
            storage=coordinator.storage,
            runtime_revision=coordinator.runtime_revision,
            capabilities=RuntimeCapabilities(
                inner_os_enabled=self._settings.meeting.inner_os_enabled,
                inner_os_analysis_enabled=self._settings.meeting.inner_os_analysis_enabled,
                inner_os_channel="loopback_only",
            ),
            audio_levels=AudioLevelsSnapshot(
                microphone=audio_levels.microphone,
                physical_output=audio_levels.physical_output,
                mixed=audio_levels.mixed,
                updated_at_ns=audio_levels.updated_at_ns,
            ),
        )

    def diagnostics(self) -> dict[str, Any]:
        """返回不含内部队列和音频内容的运行时诊断快照。"""
        audio_levels = self._audio_levels.snapshot()
        return {
            "audio_hub": self.hub.sink_diagnostics(),
            "interaction": {
                "queued_chunks": self.audio_queue.qsize(),
                "dropped_chunks": self._interaction_dropped_chunks,
            },
            "subtitles": self.subtitle_proxy.diagnostics(self._coordinator.pcm_owner),
            "tts": self.observer.tts_source_diagnostics,
            "audio_levels": {
                "microphone": audio_levels.microphone,
                "physical_output": audio_levels.physical_output,
                "mixed": audio_levels.mixed,
                "updated_at_ns": audio_levels.updated_at_ns,
            },
            "last_transition": self._coordinator.last_transition,
        }

    @property
    def mode(self) -> RuntimeMode:
        return self._coordinator.mode

    @property
    def active_meeting_id(self) -> Any | None:
        return self._coordinator.active_meeting_id

    @property
    def meeting_state(self) -> Any | None:
        return self._coordinator.meeting_state

    async def start_meeting(
        self, title: str | None = None, max_speakers: int | None = None
    ) -> Any:
        if max_speakers is not None:
            return await self._coordinator.start_meeting(title, max_speakers=max_speakers)
        return await self._coordinator.start_meeting(title)

    async def end_meeting(self, meeting_id: str | None = None) -> Any:
        return await self._coordinator.end_meeting(meeting_id)

    async def start_assistant(self) -> None:
        await self._coordinator.start_assistant()

    async def start_subtitles(self) -> None:
        await self._coordinator.start_subtitles()

    async def stop_active_mode(self) -> None:
        await self._coordinator.stop_active_mode()

    def _publish_runtime_state(self) -> None:
        runtime_events = getattr(self, "runtime_events", None)
        if runtime_events is not None:
            runtime_events.publish(self.snapshot())

    def _set_pcm_owner(self, owner: PCMOwner) -> None:
        self._pcm_owner = owner
        if owner is PCMOwner.NONE:
            self._drain_audio_queue()

    async def _start_subtitle_proxy(self) -> None:
        try:
            await self.subtitle_proxy.start()
        except Exception:
            logger.warning(
                "UIRuntime: SubtitleProxy 启动失败（SpeechRail 未就绪？）",
                exc_info=True,
            )

    def _wire_sinks(self) -> None:
        if self._sinks_wired:
            return
        self.hub.add_sink("pipecat", self._enqueue_audio)
        self.hub.add_sink("subtitle", self._push_subtitle_audio)
        self.hub.add_sink("levels", self._observe_mic_audio)
        self._sinks_wired = True

    async def _observe_mic_audio(self, data: bytes) -> None:
        if self._audio_levels.update(AudioSourceKind.MICROPHONE, data):
            self._publish_runtime_state()

    async def _push_subtitle_audio(self, data: bytes) -> None:
        if self.hub.muted:
            return
        # 语音助手模式下，若 TTS 正在播报且未触发真人插话，阻断外放回声流向字幕服务
        if (
            self.mode is RuntimeMode.ASSISTANT
            and self.session.active
            and self.session.is_echo_suppressing(data)
        ):
            return
        if self._pcm_owner not in {PCMOwner.SUBTITLES, PCMOwner.MEETING}:
            return
        await self.subtitle_proxy.push_audio(data)

    async def _start_hub(self) -> bool:
        try:
            await self.hub.start()
            return True
        except Exception:
            logger.warning("UIRuntime: AudioHub 启动失败（麦克风不可用？）", exc_info=True)
            return False

    async def _enqueue_audio(self, data: bytes) -> None:
        if (
            self.hub.muted
            or self._pcm_owner is not PCMOwner.ASSISTANT
            or not self.session.active
        ):
            return
        try:
            self.audio_queue.put_nowait(data)
        except asyncio.QueueFull:
            self._interaction_dropped_chunks += 1

    def _drain_audio_queue(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.task_done()
            except asyncio.QueueEmpty:
                return
