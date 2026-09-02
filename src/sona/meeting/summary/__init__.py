"""会议助手的证据可追溯 AI 纪要服务包。

对外导出稳定接口，保持与原 summary.py 完全一致的命名空间与符号。
"""

from __future__ import annotations

from sona.meeting.minutes_rendering import render_minutes_markdown
from sona.meeting.summary.chunker import split_document
from sona.meeting.summary.errors import (
    InvalidEvidenceError,
    SummaryError,
    SummaryOutputLimitError,
    SummaryTimeoutError,
    SummaryUnavailableError,
    SummaryValidationError,
)
from sona.meeting.summary.evidence_anchor import (
    format_transcript,
    validate_evidence,
)
from sona.meeting.summary.model_gateway import (
    MeetingSummaryClient,
    MeetingSummaryRepository,
    SummaryClientProtocol,
)
from sona.meeting.summary.prompt_builder import (
    SUMMARY_PROMPT_VERSION,
    map_instructions,
    reduce_instructions,
    repair_instructions,
    title_instructions,
)
from sona.meeting.summary.schema_validator import (
    MinutesContent,
    SummaryArtifact,
    parse_summary_output,
)
from sona.meeting.summary.service import (
    EventPublisher,
    MeetingSummaryService,
)

__all__ = [
    "SUMMARY_PROMPT_VERSION",
    "EventPublisher",
    "InvalidEvidenceError",
    "MeetingSummaryClient",
    "MeetingSummaryRepository",
    "MeetingSummaryService",
    "MinutesContent",
    "SummaryArtifact",
    "SummaryClientProtocol",
    "SummaryError",
    "SummaryOutputLimitError",
    "SummaryTimeoutError",
    "SummaryUnavailableError",
    "SummaryValidationError",
    "format_transcript",
    "map_instructions",
    "parse_summary_output",
    "reduce_instructions",
    "render_minutes_markdown",
    "repair_instructions",
    "split_document",
    "title_instructions",
    "validate_evidence",
]
