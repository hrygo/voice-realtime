"""四种语音工作负载的两阶段互斥运行模式编排。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID

from voice_realtime.meeting.models import (
    MeetingRecord,
    MeetingStatus,
    PCMOwner,
    RuntimeMode,
    StorageHealth,
)

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
TransitionValue = str | int | float | None


class InteractionWorkload(Protocol):
    """协调器所需的交互会话最小接口。"""

    @property
    def active(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self, *, reason: str) -> None: ...


class SubtitleWorkload(Protocol):
    """普通字幕的无 PCM prepare / 同步 commit 接口。"""

    @property
    def browser_capture_active(self) -> bool: ...

    async def prepare_browser_capture(self, *, timeout_secs: float) -> Any: ...

    def commit_browser_capture(self, preparation: Any) -> None: ...

    async def abort_browser_capture(self, preparation: Any) -> None: ...

    async def deactivate_browser_capture(self) -> None: ...


class MeetingWorkload(Protocol):
    """会议会话的两阶段启动及 EOF 停止接口。"""

    @property
    def active_meeting_id(self) -> UUID | None: ...

    @property
    def record(self) -> MeetingRecord | None: ...

    async def prepare_start(self, title: str | None = None) -> Any: ...

    def commit_start(self, preparation: Any) -> MeetingRecord: ...

    async def publish_started(self, preparation: Any) -> None: ...

    async def abort_start(self, preparation: Any) -> None: ...

    async def stop(self) -> MeetingRecord: ...

    async def interrupt(self, reason: str) -> None: ...


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
    """以单锁和两阶段屏障串行仲裁所有麦克风推理工作负载。"""

    def __init__(
        self,
        interaction: InteractionWorkload,
        subtitles: SubtitleWorkload | MeetingWorkload | None = None,
        meeting: MeetingWorkload | None = None,
        *,
        meeting_session: MeetingWorkload | None = None,
        initial_mode: RuntimeMode | str | None = None,
        on_owner_changed: Callable[[PCMOwner], None] | None = None,
        state_publisher: Callable[[], None] | None = None,
        subtitle_prepare_timeout_secs: float = 5.0,
    ) -> None:
        explicit_meeting = meeting_session if meeting_session is not None else meeting
        subtitle_workload: SubtitleWorkload | None
        if subtitles is not None and not self._declares_subtitle_interface(subtitles):
            if explicit_meeting is not None:
                raise TypeError("meeting session 被重复提供")
            explicit_meeting = cast(MeetingWorkload, subtitles)
            subtitle_workload = None
        else:
            subtitle_workload = cast(SubtitleWorkload | None, subtitles)

        if subtitle_prepare_timeout_secs <= 0:
            raise ValueError("subtitle_prepare_timeout_secs 必须大于 0")
        self.interaction = interaction
        self.subtitles = subtitle_workload
        self.meeting = explicit_meeting
        if initial_mode is None:
            initial_mode = (
                RuntimeMode.ASSISTANT if bool(interaction.active) else RuntimeMode.IDLE
            )
        self._mode = RuntimeMode(initial_mode)
        self._pcm_owner = self._owner_for_mode(self._mode)
        self._active_meeting_id: UUID | None = None
        self._meeting_record: MeetingRecord | None = None
        self._command_lock = asyncio.Lock()
        self._transition_task: asyncio.Task[Any] | None = None
        self._prepared_target: tuple[RuntimeMode, Any, bool] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._closing = False
        self._stopped = False
        self._runtime_revision = 0
        self._on_owner_changed = on_owner_changed or (lambda _owner: None)
        self._state_publisher = state_publisher or (lambda: None)
        self._subtitle_prepare_timeout_secs = subtitle_prepare_timeout_secs
        self._last_transition: dict[str, TransitionValue] | None = None
        self._rollback_result: str | None = None
        self._rollback_error_type: str | None = None

    @staticmethod
    def _declares_subtitle_interface(workload: object) -> bool:
        """区分新 subtitle positional 参数与旧 positional meeting 调用。"""
        method_names = (
            "prepare_browser_capture",
            "commit_browser_capture",
            "abort_browser_capture",
            "deactivate_browser_capture",
        )
        return all(callable(getattr(workload, name, None)) for name in method_names)

    @staticmethod
    def _owner_for_mode(mode: RuntimeMode) -> PCMOwner:
        if mode is RuntimeMode.IDLE:
            return PCMOwner.NONE
        return PCMOwner(mode.value)

    @property
    def mode(self) -> RuntimeMode:
        return self._mode

    @property
    def pcm_owner(self) -> PCMOwner:
        return self._pcm_owner

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

    @property
    def last_transition(self) -> dict[str, TransitionValue] | None:
        return self._last_transition

    def configure_meeting(self, meeting_session: MeetingWorkload) -> None:
        """后注入一次会议服务，不改变现有仲裁状态或订阅者。"""
        if self.meeting is not None:
            raise RuntimeError("meeting runtime 已配置")
        self.meeting = meeting_session

    async def start_assistant(self) -> None:
        await self._run_serialized(RuntimeMode.ASSISTANT, self._start_assistant_locked)

    async def start_subtitles(self) -> None:
        await self._run_serialized(RuntimeMode.SUBTITLES, self._start_subtitles_locked)

    async def start_meeting(self, title: str | None = None) -> MeetingRecord:
        meeting = self.meeting
        if meeting is None:
            raise MeetingUnavailable("meeting service unavailable")

        async def command() -> tuple[Any, MeetingRecord]:
            return await self._start_meeting_locked(title)

        async def publish_started(result: tuple[Any, MeetingRecord]) -> None:
            preparation, _record = result
            await self._publish_committed_meeting_started(meeting, preparation)

        _preparation, record = await self._run_serialized(
            RuntimeMode.MEETING,
            command,
            after_commit=publish_started,
        )
        return record

    async def restart_assistant(
        self, restart: Callable[[], Awaitable[None]]
    ) -> None:
        """在模式命令锁内复核 assistant 所有权并执行重启。"""

        async def command() -> None:
            if (
                self.mode is not RuntimeMode.ASSISTANT
                or self.pcm_owner is not PCMOwner.ASSISTANT
            ):
                raise ModeConflict("仅助手模式允许重启语音管道")
            await restart()

        await self._run_serialized(RuntimeMode.ASSISTANT, command)

    async def end_meeting(self, meeting_id: UUID | str | None = None) -> MeetingRecord:
        async def command() -> MeetingRecord:
            return await self._end_meeting_locked(meeting_id)

        return await self._run_serialized(RuntimeMode.IDLE, command)

    async def stop_active_mode(self) -> None:
        await self._run_serialized(RuntimeMode.IDLE, self._stop_active_mode_locked)

    async def _run_serialized(
        self,
        target: RuntimeMode,
        command: Callable[[], Awaitable[T]],
        *,
        after_commit: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        async with self._command_lock:
            if self._closing:
                raise MeetingUnavailable("runtime 正在关闭")
            current = asyncio.current_task()
            if current is None:
                raise RuntimeError("runtime transition 缺少 asyncio task")
            self._transition_task = current
            self._rollback_result = None
            self._rollback_error_type = None
            started = time.monotonic()
            try:
                result = await command()
            except asyncio.CancelledError as exc:
                await self._abort_prepared_target_safely()
                self._record_transition(target, started, "cancelled", exc)
                raise
            except Exception as exc:
                self._record_transition(target, started, "failed", exc)
                raise
            else:
                self._record_transition(target, started, "success", None)
                if after_commit is not None:
                    # commit 已完成；此处异常不得进入上方 pre-commit 补偿分支。
                    await after_commit(result)
                return result
            finally:
                if self._transition_task is current:
                    self._transition_task = None

    def _record_transition(
        self,
        target: RuntimeMode,
        started: float,
        result: str,
        error: BaseException | None,
    ) -> None:
        snapshot: dict[str, TransitionValue] = {
            "target": target.value,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "result": result,
            "rollback_result": self._rollback_result,
        }
        if error is not None:
            snapshot["error_type"] = type(error).__name__
            snapshot["error_code"] = str(
                getattr(error, "code", MeetingUnavailableError.code)
            )
        if self._rollback_error_type is not None:
            snapshot["rollback_error_type"] = self._rollback_error_type
        self._last_transition = snapshot

    async def _start_assistant_locked(self) -> None:
        if self._mode is RuntimeMode.MEETING:
            raise ModeConflict("会议录制期间不能启动语音助手")
        if self._mode is RuntimeMode.ASSISTANT and bool(self.interaction.active):
            return

        async def prepare() -> object:
            try:
                await self.interaction.start()
            except Exception:
                if bool(self.interaction.active):
                    with contextlib.suppress(Exception):
                        await self.interaction.stop(reason="目标准备失败")
                raise
            return self.interaction

        def commit(_preparation: object) -> None:
            return None

        async def abort(_preparation: object) -> None:
            if bool(self.interaction.active):
                await self.interaction.stop(reason="取消模式切换")

        await self._switch_workload(
            RuntimeMode.ASSISTANT,
            source=(
                RuntimeMode.IDLE
                if self._mode is RuntimeMode.ASSISTANT
                else self._mode
            ),
            prepare=prepare,
            commit=commit,
            abort=abort,
        )

    async def _start_subtitles_locked(self) -> None:
        if self._mode is RuntimeMode.MEETING:
            raise ModeConflict("会议录制期间不能启动普通字幕")
        subtitles = self.subtitles
        if subtitles is None:
            raise MeetingUnavailable("字幕服务不可用")
        if self._mode is RuntimeMode.SUBTITLES and bool(
            subtitles.browser_capture_active
        ):
            return

        async def prepare() -> Any:
            return await subtitles.prepare_browser_capture(
                timeout_secs=self._subtitle_prepare_timeout_secs
            )

        def commit(preparation: Any) -> None:
            subtitles.commit_browser_capture(preparation)

        async def abort(preparation: Any) -> None:
            await subtitles.abort_browser_capture(preparation)

        await self._switch_workload(
            RuntimeMode.SUBTITLES,
            source=(
                RuntimeMode.IDLE
                if self._mode is RuntimeMode.SUBTITLES
                else self._mode
            ),
            prepare=prepare,
            commit=commit,
            abort=abort,
        )

    async def _start_meeting_locked(
        self, title: str | None
    ) -> tuple[Any, MeetingRecord]:
        if self._mode is RuntimeMode.MEETING:
            raise ModeConflict("meeting 已经在录制")
        meeting = self.meeting
        if meeting is None:
            raise MeetingUnavailable("meeting service unavailable")

        async def prepare() -> Any:
            return await meeting.prepare_start(title)

        def commit(preparation: Any) -> MeetingRecord:
            record = meeting.commit_start(preparation)
            self._meeting_record = record
            self._active_meeting_id = record.id
            return record

        async def abort(preparation: Any) -> None:
            await meeting.abort_start(preparation)

        return await self._switch_workload(
            RuntimeMode.MEETING,
            source=self._mode,
            prepare=prepare,
            commit=commit,
            abort=abort,
        )

    @staticmethod
    async def _publish_committed_meeting_started(
        meeting: MeetingWorkload, preparation: Any
    ) -> None:
        """屏蔽调用者取消直至已提交会议的 started 事件发布完成。"""
        publish_task = asyncio.create_task(meeting.publish_started(preparation))
        cancellation: asyncio.CancelledError | None = None
        while not publish_task.done():
            try:
                await asyncio.shield(publish_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break

        if publish_task.cancelled():
            if cancellation is not None:
                raise cancellation
            await publish_task
        else:
            try:
                publish_task.result()
            except Exception as exc:
                LOGGER.error(
                    "meeting started event publish failed",
                    extra={"error_type": type(exc).__name__},
                )
        if cancellation is not None:
            raise cancellation

    async def _switch_workload(
        self,
        target: RuntimeMode,
        *,
        source: RuntimeMode,
        prepare: Callable[[], Awaitable[Any]],
        commit: Callable[[Any], T],
        abort: Callable[[Any], Awaitable[None]],
    ) -> tuple[Any, T]:
        preparation = await prepare()
        target_committed = False
        self._prepared_target = (target, preparation, target_committed)
        try:
            if source is not RuntimeMode.IDLE:
                self._set_transition_barrier()
            await self._quiesce_source(source)
            result = commit(preparation)
            target_committed = True
            self._prepared_target = (target, preparation, target_committed)
            self._commit_state(target, self._owner_for_mode(target))
            self._prepared_target = None
        except asyncio.CancelledError as cancellation:
            abort_error = await self._abort_target(
                target,
                preparation,
                abort,
                committed=target_committed,
                suppress_errors=True,
            )
            if self._closing:
                raise
            if abort_error is not None:
                await self._force_idle_state(cancellation, abort_error)
                raise
            try:
                await self._restore_source(source)
                self._rollback_result = "success"
                self._commit_state(source, self._owner_for_mode(source))
            except Exception as rollback_error:
                await self._force_idle_state(cancellation, rollback_error)
            raise
        except Exception as source_error:
            abort_error = await self._abort_target(
                target,
                preparation,
                abort,
                committed=target_committed,
                suppress_errors=False,
            )
            if abort_error is not None:
                await self._force_idle(source_error, abort_error)
            await self._recover_source_or_force_idle(source, source_error)
            raise
        return preparation, result

    async def _abort_target(
        self,
        target: RuntimeMode,
        preparation: Any,
        abort: Callable[[Any], Awaitable[None]],
        *,
        committed: bool,
        suppress_errors: bool,
    ) -> Exception | None:
        try:
            if committed:
                await self._deactivate_target(target)
            else:
                await abort(preparation)
        except Exception as exc:
            if suppress_errors:
                LOGGER.error(
                    "target abort failed",
                    extra={"target": target.value, "error_type": type(exc).__name__},
                )
            return exc
        self._prepared_target = None
        return None

    async def _deactivate_target(self, target: RuntimeMode) -> None:
        if target is RuntimeMode.ASSISTANT:
            if bool(self.interaction.active):
                await self.interaction.stop(reason="取消模式切换")
            return
        if target is RuntimeMode.SUBTITLES:
            if self.subtitles is not None:
                await self.subtitles.deactivate_browser_capture()
            return
        if target is RuntimeMode.MEETING and self.meeting is not None:
            await self.meeting.interrupt("mode_switch_aborted")
            self._active_meeting_id = None
            latest_record = getattr(self.meeting, "record", None)
            if latest_record is not None:
                self._meeting_record = latest_record

    async def _recover_source_or_force_idle(
        self,
        source: RuntimeMode,
        source_error: Exception,
    ) -> None:
        try:
            await self._restore_source(source)
            self._rollback_result = "success"
            self._commit_state(source, self._owner_for_mode(source))
        except Exception as rollback_error:
            await self._force_idle(source_error, rollback_error)

    async def _force_idle(
        self,
        source_error: BaseException,
        rollback_error: Exception,
    ) -> None:
        await self._force_idle_state(source_error, rollback_error)
        raise MeetingUnavailable("运行时工作负载恢复失败") from source_error

    async def _force_idle_state(
        self,
        source_error: BaseException,
        rollback_error: Exception,
    ) -> None:
        self._rollback_result = "failed"
        self._rollback_error_type = type(rollback_error).__name__
        LOGGER.error(
            "runtime transition recovery failed",
            extra={
                "source_error_type": type(source_error).__name__,
                "rollback_error_type": type(rollback_error).__name__,
                "error_code": MeetingUnavailableError.code,
            },
        )
        await self._stop_all_workloads()
        self._active_meeting_id = None
        self._force_commit_idle()

    async def _quiesce_source(self, source: RuntimeMode) -> None:
        if source is RuntimeMode.ASSISTANT:
            await self.interaction.stop(reason="切换运行时模式")
        elif source is RuntimeMode.SUBTITLES:
            subtitles = self.subtitles
            if subtitles is None:
                raise RuntimeError("字幕来源工作负载不存在")
            await subtitles.deactivate_browser_capture()

    async def _restore_source(self, source: RuntimeMode) -> None:
        if source is RuntimeMode.ASSISTANT:
            if not bool(self.interaction.active):
                await self.interaction.start()
            if not bool(self.interaction.active):
                raise RuntimeError("assistant source 未恢复")
            return
        if source is RuntimeMode.SUBTITLES:
            subtitles = self.subtitles
            if subtitles is None:
                raise RuntimeError("subtitle source 不存在")
            if not bool(subtitles.browser_capture_active):
                preparation = await subtitles.prepare_browser_capture(
                    timeout_secs=self._subtitle_prepare_timeout_secs
                )
                subtitles.commit_browser_capture(preparation)
            if not bool(subtitles.browser_capture_active):
                raise RuntimeError("subtitle source 未恢复")

    async def _end_meeting_locked(
        self, meeting_id: UUID | str | None
    ) -> MeetingRecord:
        meeting = self.meeting
        if (
            self._mode is not RuntimeMode.MEETING
            or self._active_meeting_id is None
            or meeting is None
        ):
            raise MeetingNotActive("没有正在录制的会议")
        expected = self._active_meeting_id
        if meeting_id is not None and str(meeting_id) != str(expected):
            raise MeetingNotActive("会议 ID 不匹配")
        try:
            self._set_transition_barrier()
        except Exception as barrier_error:
            self._rollback_result = "success"
            try:
                self._commit_state(RuntimeMode.MEETING, PCMOwner.MEETING)
            except Exception as rollback_error:
                await self._force_idle(barrier_error, rollback_error)
            raise
        try:
            record = await meeting.stop()
        except BaseException as stop_error:
            latest_record = getattr(meeting, "record", None)
            if latest_record is not None:
                self._meeting_record = latest_record
            elif self._meeting_record is not None:
                self._meeting_record = self._meeting_record.model_copy(
                    update={
                        "status": MeetingStatus.INTERRUPTED,
                        "interruption_reason": "meeting_stop_failed",
                    }
                )
            self._active_meeting_id = None
            try:
                self._commit_state(RuntimeMode.IDLE, PCMOwner.NONE)
            except Exception as callback_error:
                await self._force_idle_state(stop_error, callback_error)
            raise
        self._meeting_record = record
        self._active_meeting_id = None
        try:
            self._commit_state(RuntimeMode.IDLE, PCMOwner.NONE)
        except Exception as callback_error:
            await self._force_idle(callback_error, callback_error)
        return record

    async def _stop_active_mode_locked(self) -> None:
        if self._mode is RuntimeMode.IDLE:
            return
        if self._mode is RuntimeMode.MEETING:
            await self._end_meeting_locked(None)
            return
        source = self._mode
        try:
            self._set_transition_barrier()
            await self._quiesce_source(source)
            self._commit_state(RuntimeMode.IDLE, PCMOwner.NONE)
        except asyncio.CancelledError as cancellation:
            if self._closing:
                raise
            try:
                await self._restore_source(source)
                self._rollback_result = "success"
                self._commit_state(source, self._owner_for_mode(source))
            except Exception as rollback_error:
                await self._force_idle_state(cancellation, rollback_error)
            raise
        except Exception as source_error:
            if self._source_active(source):
                self._rollback_result = "success"
                try:
                    self._commit_state(source, self._owner_for_mode(source))
                except Exception as rollback_error:
                    await self._force_idle(source_error, rollback_error)
            else:
                await self._force_idle_state(source_error, source_error)
            raise source_error

    def _source_active(self, source: RuntimeMode) -> bool:
        if source is RuntimeMode.ASSISTANT:
            return bool(self.interaction.active)
        if source is RuntimeMode.SUBTITLES:
            return self.subtitles is not None and bool(
                self.subtitles.browser_capture_active
            )
        if source is RuntimeMode.MEETING:
            return self._active_meeting_id is not None
        return False

    def _set_transition_barrier(self) -> None:
        previous_owner = self._pcm_owner
        self._pcm_owner = PCMOwner.NONE
        try:
            self._on_owner_changed(PCMOwner.NONE)
        except Exception:
            self._pcm_owner = previous_owner
            self._restore_owner_callback(previous_owner)
            raise

    def _commit_state(self, mode: RuntimeMode, owner: PCMOwner) -> None:
        previous_mode = self._mode
        previous_owner = self._pcm_owner
        self._mode = mode
        self._pcm_owner = owner
        try:
            self._on_owner_changed(owner)
        except Exception:
            self._mode = previous_mode
            self._pcm_owner = previous_owner
            self._restore_owner_callback(previous_owner)
            raise
        self._runtime_revision += 1
        self._publish_state()

    def _force_commit_idle(self) -> None:
        self._mode = RuntimeMode.IDLE
        self._pcm_owner = PCMOwner.NONE
        try:
            self._on_owner_changed(PCMOwner.NONE)
        except Exception as exc:
            LOGGER.error(
                "runtime owner callback failed during forced idle",
                extra={"error_type": type(exc).__name__},
            )
        self._runtime_revision += 1
        self._publish_state()

    def _restore_owner_callback(self, owner: PCMOwner) -> None:
        try:
            self._on_owner_changed(owner)
        except Exception as exc:
            LOGGER.error(
                "runtime owner callback rollback failed",
                extra={"error_type": type(exc).__name__},
            )

    def _publish_state(self) -> None:
        try:
            self._state_publisher()
        except Exception as exc:
            LOGGER.error(
                "runtime state publish failed",
                extra={"error_type": type(exc).__name__},
            )

    async def _abort_prepared_target_safely(self) -> None:
        prepared = self._prepared_target
        if prepared is None:
            return
        target, preparation, committed = prepared
        try:
            if committed:
                await self._deactivate_target(target)
            elif target is RuntimeMode.SUBTITLES and self.subtitles is not None:
                await self.subtitles.abort_browser_capture(preparation)
            elif target is RuntimeMode.MEETING and self.meeting is not None:
                await self.meeting.abort_start(preparation)
            elif target is RuntimeMode.ASSISTANT and bool(self.interaction.active):
                await self.interaction.stop(reason="取消模式切换")
        except Exception as exc:
            LOGGER.error(
                "prepared target abort failed",
                extra={"target": target.value, "error_type": type(exc).__name__},
            )
        else:
            self._prepared_target = None

    async def _stop_all_workloads(self) -> None:
        await self._abort_prepared_target_safely()
        if bool(self.interaction.active):
            with contextlib.suppress(Exception):
                await self.interaction.stop(reason="应用停止")
        if self.subtitles is not None:
            with contextlib.suppress(Exception):
                await self.subtitles.deactivate_browser_capture()
        if self.meeting is not None and (
            self._active_meeting_id is not None
            or getattr(self.meeting, "active_meeting_id", None) is not None
        ):
            with contextlib.suppress(Exception):
                await self.meeting.interrupt("应用停止")

    async def stop(self) -> None:
        """取消在途 prepare，尽力全停并发布最终 idle/none 快照。"""
        if self._stopped:
            return
        shutdown = self._shutdown_task
        if shutdown is None:
            self._closing = True
            shutdown = asyncio.create_task(self._stop_once())
            self._shutdown_task = shutdown
        await asyncio.shield(shutdown)

    async def _stop_once(self) -> None:
        """执行唯一一次关闭，供所有并发 stop 调用共享。"""
        revision_before_shutdown = self._runtime_revision
        self._closing = True
        current = asyncio.current_task()
        transition = self._transition_task
        if (
            transition is not None
            and transition is not current
            and not transition.done()
        ):
            transition.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await transition
        async with self._command_lock:
            await self._stop_all_workloads()
            self._active_meeting_id = None
            shutdown_already_committed = (
                self._mode is RuntimeMode.IDLE
                and self._pcm_owner is PCMOwner.NONE
                and self._runtime_revision > revision_before_shutdown
            )
            if not shutdown_already_committed:
                self._force_commit_idle()
            self._stopped = True
