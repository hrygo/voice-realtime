"""会议纪要 JSON Schema 解析、容错清洗与 Artifact 封装。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from sona.meeting.models import MinutesResult
from sona.meeting.summary.errors import (
    InvalidEvidenceError,
    SummaryValidationError,
)
from sona.meeting.summary.prompt_builder import SUMMARY_PROMPT_VERSION
from sona.meeting.summary_contract import (
    ModelMapMinutesResult,
    ModelMinutesResult,
    resolve_minutes_result,
)

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)

# ``MinutesResult`` 是 Workstream A 的公共模型。别名保留了一个更适合纪要
# 层的名字，避免前端或调用方必须知道数据库模型的命名。
MinutesContent = MinutesResult


class SummaryArtifact(BaseModel):
    """传递给 Repository 的经过验证的纪要结果。

    Repository 侧可以直接把 ``content_json`` 写入 ``meeting_minutes``；保留
    ``raw_output`` 仅为兼容格式失败诊断，正常完成时始终为 ``None``。
    """

    model_config = ConfigDict(extra="forbid")

    content_json: MinutesContent
    content_markdown: str
    model: str
    prompt_version: str = SUMMARY_PROMPT_VERSION
    raw_output: str | None = None


def _json_candidate(raw: str) -> str:
    text = raw.strip()
    match = _CODE_FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _parse_model_output(
    raw: str,
    references: Mapping[str, UUID],
    *,
    for_map: bool = False,
) -> MinutesContent:
    try:
        model_contract = ModelMapMinutesResult if for_map else ModelMinutesResult
        model_result = model_contract.model_validate_json(_json_candidate(raw))
        return resolve_minutes_result(model_result, references)
    except ValidationError as exc:
        error = SummaryValidationError("LM Studio 输出不符合会议纪要 schema")
        error.raw_output = raw
        raise error from exc
    except ValueError as exc:
        error = InvalidEvidenceError(str(exc))
        error.raw_output = raw
        raise error from exc


def parse_summary_output(raw: Any) -> MinutesContent:
    """解析并严格校验模型 JSON，不接受自由 Markdown 作为正式结果。"""

    if isinstance(raw, MinutesContent):
        return raw
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    try:
        if isinstance(raw, Mapping):
            return MinutesContent.model_validate(dict(raw))
        if isinstance(raw, str):
            try:
                return MinutesContent.model_validate_json(_json_candidate(raw))
            except ValidationError as exc:
                error = SummaryValidationError("LM Studio 输出不符合会议纪要 schema")
                error.raw_output = raw
                raise error from exc
    except ValidationError as exc:
        raise SummaryValidationError("LM Studio 输出不符合会议纪要 schema") from exc
    raise SummaryValidationError("LM Studio 输出必须是 JSON 对象")
