"""转录对账与 RecoveryJournal 降级回放的持久化服务。

只处理 transcript/recovery：不调用 capture gateway、不发布 UI event、
不 finalize meeting。fatal persistence error 由调用方负责 abort capture。
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from .models import TranscriptReconcileResult, TranscriptWindow
from .ports import RecoveryReplayRepository, TranscriptStore

logger = logging.getLogger(__name__)


class RecoveryJournalPort(Protocol):
    """TranscriptPersistence 需要的 journal 窄端口。"""

    async def append(self, meeting_id: UUID, window: TranscriptWindow) -> object: ...

    async def replay_meeting(
        self, repository: RecoveryReplayRepository, meeting_id: UUID
    ) -> int: ...


class _PersistableRepository(TranscriptStore, RecoveryReplayRepository, Protocol):
    """reconcile 与 journal 回放共用的 repository 消费面。"""


class TranscriptPersistence:
    """在线 window 对账 + journal fallback/replay。

    ``reconcile`` 对相同 signature 去重；repository 失败时降级写 journal 并
    返回 ``None``（无可发布结果）；journal 也失败时向上抛出。
    """

    def __init__(
        self,
        repository: _PersistableRepository,
        *,
        journal: RecoveryJournalPort | None = None,
        replay_repository: RecoveryReplayRepository | None = None,
    ) -> None:
        self._transcripts = repository
        self._journal = journal
        self._replay_repository = replay_repository or repository
        self._degraded = False
        self._last_window_signatures: dict[UUID, tuple[object, ...]] = {}

    @property
    def degraded(self) -> bool:
        return self._degraded

    async def reconcile(
        self, meeting_id: UUID, window: TranscriptWindow
    ) -> TranscriptReconcileResult | None:
        signature = (
            window.source_epoch,
            tuple(
                (
                    segment.id,
                    segment.start_ms,
                    segment.end_ms,
                    segment.speaker_key,
                    segment.text,
                )
                for segment in window.segments
            ),
        )
        if signature == self._last_window_signatures.get(meeting_id):
            return None
        try:
            await self.replay_pending(meeting_id)
            result = await self._transcripts.reconcile_window(meeting_id, window)
        except Exception as exc:
            self._degraded = True
            logger.warning(
                "TranscriptPersistence: 数据库对账失败，已降级至 RecoveryJournal"
                " (meeting_id=%s): %s",
                meeting_id,
                exc,
                exc_info=True,
            )
            if self._journal is None:
                raise
            try:
                await self._journal.append(meeting_id, window)
            except Exception:
                logger.exception(
                    "TranscriptPersistence: journal 写入失败 (meeting_id=%s)",
                    meeting_id,
                )
                raise
            return None
        self._last_window_signatures[meeting_id] = signature
        self._degraded = False
        return result

    async def replay_pending(self, meeting_id: UUID) -> int:
        journal = self._journal
        if journal is None:
            return 0
        count = await journal.replay_meeting(self._replay_repository, meeting_id)
        if count > 0:
            logger.info(
                "TranscriptPersistence: 重放未处理 Journal 记录 (meeting_id=%s, count=%d)",
                meeting_id,
                count,
            )
            self._degraded = False
        return count


__all__ = ["RecoveryJournalPort", "TranscriptPersistence"]
