"""语音助手与会议助手的互斥运行模式编排。"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from voice_realtime.meeting.models import MeetingRecord, MeetingStatus, RuntimeMode, StorageHealth


class RuntimeModeError(RuntimeError):
    """可安全暴露给控制协议的模式错误。"""

    code = "command_failed"


class ModeConflictError(RuntimeModeError):
    code = "mode_conflict"


class MeetingNotActiveError(RuntimeModeError):
    code = "meeting_not_active"


class MeetingUnavailableError(RuntimeModeError):
    code = "service_unavailable"


# Short aliases are kept for callers that want the stable domain names.
ModeConflict = ModeConflictError
MeetingNotActive = MeetingNotActiveError
MeetingUnavailable = MeetingUnavailableError


class RuntimeModeCoordinator:
    """串行协调 InteractionSession 与 MeetingSession 的生命周期。"""

    def __init__(
        self,
        interaction: Any,
        meeting: Any | None = None,
        *,
        meeting_session: Any | None = None,
        initial_mode: RuntimeMode | str | None = None,
    ) -> None:
        if meeting is None:
            meeting = meeting_session
        if meeting is None:
            raise ValueError("meeting session 不能为空")
        self.interaction = interaction
        self.meeting = meeting
        if initial_mode is None:
            initial_mode = (
                RuntimeMode.ASSISTANT
                if bool(getattr(interaction, "active", False))
                else RuntimeMode.IDLE
            )
        self._mode = RuntimeMode(initial_mode)
        self._active_meeting_id: UUID | None = None
        self._meeting_record: MeetingRecord | None = None
        self._lock = asyncio.Lock()
        self._runtime_revision = 0

    @property
    def mode(self) -> RuntimeMode:
        return self._mode

    @property
    def active_meeting_id(self) -> UUID | None:
        return self._active_meeting_id

    @property
    def meeting_record(self) -> MeetingRecord | None:
        return self._meeting_record

    @property
    def meeting_state(self) -> MeetingStatus | None:
        record = self._meeting_record
        return record.status if record is not None else None

    @property
    def meeting_started_at(self) -> datetime | None:
        record = self._meeting_record
        return record.started_at if record is not None else None

    @property
    def storage(self) -> StorageHealth:
        value = getattr(self.meeting, "storage_health", StorageHealth.OK)
        try:
            return StorageHealth(value)
        except (TypeError, ValueError):
            return StorageHealth.OK

    @property
    def runtime_revision(self) -> int:
        return self._runtime_revision

    async def start_meeting(self, title: str | None = None) -> MeetingRecord:
        async with self._lock:
            if self._mode is RuntimeMode.MEETING:
                raise ModeConflict("meeting 已经在录制")
            if self._mode is not RuntimeMode.ASSISTANT and self._mode is not RuntimeMode.IDLE:
                raise ModeConflict("当前模式不可开始会议")
            was_active = bool(getattr(self.interaction, "active", False))
            if was_active:
                await self.interaction.stop(reason="切换至会议模式")
            try:
                record = await self.meeting.start(title or "")
            except Exception:
                if was_active:
                    await self.interaction.start()
                    self._mode = RuntimeMode.ASSISTANT
                raise
            self._meeting_record = record
            self._active_meeting_id = record.id
            self._mode = RuntimeMode.MEETING
            self._runtime_revision += 1
            return cast(MeetingRecord, record)

    async def end_meeting(self, meeting_id: UUID | str | None = None) -> MeetingRecord:
        async with self._lock:
            if self._mode is not RuntimeMode.MEETING or self._active_meeting_id is None:
                raise MeetingNotActive("没有正在录制的会议")
            expected = self._active_meeting_id
            if meeting_id is not None and str(meeting_id) != str(expected):
                raise MeetingNotActive("会议 ID 不匹配")
            try:
                record = await self.meeting.stop()
            finally:
                # 会后必须保持 idle；不得因为结束成功而自动重启语音助手。
                self._mode = RuntimeMode.IDLE
                self._active_meeting_id = None
                self._runtime_revision += 1
            self._meeting_record = record
            return cast(MeetingRecord, record)

    async def start_assistant(self) -> None:
        async with self._lock:
            if self._mode is RuntimeMode.MEETING:
                raise ModeConflict("会议录制期间不能启动语音助手")
            if self._mode is RuntimeMode.ASSISTANT and bool(
                getattr(self.interaction, "active", False)
            ):
                return
            await self.interaction.start()
            self._mode = RuntimeMode.ASSISTANT
            self._runtime_revision += 1

    async def stop_active_mode(self) -> None:
        async with self._lock:
            if self._mode is RuntimeMode.MEETING:
                # 避免重新进入锁；当前方法已经负责串行化。
                expected = self._active_meeting_id
                record = self._meeting_record
                try:
                    record = await self.meeting.stop()
                finally:
                    self._mode = RuntimeMode.IDLE
                    self._active_meeting_id = None
                    self._runtime_revision += 1
                if expected is not None:
                    self._meeting_record = cast(MeetingRecord, record)
                return
            if self._mode is RuntimeMode.ASSISTANT or bool(
                getattr(self.interaction, "active", False)
            ):
                await self.interaction.stop(reason="停止当前模式")
            self._mode = RuntimeMode.IDLE
            self._runtime_revision += 1

    async def stop(self) -> None:
        """应用关闭时释放当前资源；会议中断由 MeetingSession 保留记录。"""
        async with self._lock:
            if self._mode is RuntimeMode.MEETING:
                with contextlib.suppress(Exception):
                    await self.meeting.interrupt("应用停止")
                self._mode = RuntimeMode.IDLE
                self._active_meeting_id = None
            elif bool(getattr(self.interaction, "active", False)):
                await self.interaction.stop(reason="应用停止")
            self._runtime_revision += 1
