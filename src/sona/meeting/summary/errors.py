"""会议纪要领域异常定义。"""

from __future__ import annotations


class SummaryError(RuntimeError):
    """纪要任务错误的共同基类。"""

    code = "summary_unavailable"


class SummaryUnavailableError(SummaryError):
    """LM Studio 不可用或返回传输错误。"""

    code = "summary_unavailable"


class SummaryTimeoutError(SummaryUnavailableError):
    """模型调用或整条纪要任务超过 wall-clock deadline。"""

    code = "summary_timeout"


class SummaryOutputLimitError(SummaryUnavailableError):
    """模型持续输出退化内容，超过客户端字符安全阈值。"""

    code = "output_limit"


class SummaryValidationError(SummaryError, ValueError):
    """模型输出不是合约规定的结构。"""

    code = "invalid_schema"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.raw_output: str | None = None


class InvalidEvidenceError(SummaryValidationError):
    """纪要引用了不属于当前会议的 segment UUID。"""

    code = "invalid_evidence"
