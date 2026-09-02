"""会议助手后端领域层。

该包只承载与前端无关的会议状态、转录和纪要数据契约；运行时编排、HTTP
路由和 React 实现位于各自的边界模块中。
"""

from .models import (
    ActionItem,
    Decision,
    Highlight,
    MeetingPage,
    MeetingRecord,
    MeetingStatus,
    MinutesJob,
    MinutesRecord,
    MinutesResult,
    MinutesStatus,
    NormalizedSegment,
    OpenQuestion,
    Risk,
    RuntimeMode,
    SpeakerRecord,
    StorageHealth,
    Topic,
    TranscriptDocument,
    TranscriptReconcileResult,
    TranscriptWindow,
)

__all__ = [
    "ActionItem",
    "Decision",
    "Highlight",
    "MeetingPage",
    "MeetingRecord",
    "MeetingStatus",
    "MinutesJob",
    "MinutesRecord",
    "MinutesResult",
    "MinutesStatus",
    "NormalizedSegment",
    "OpenQuestion",
    "Risk",
    "RuntimeMode",
    "SpeakerRecord",
    "StorageHealth",
    "Topic",
    "TranscriptDocument",
    "TranscriptReconcileResult",
    "TranscriptWindow",
]
