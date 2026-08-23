"""中文流式首句极速弱标点分词聚合器（降低 TTS 首字发音延迟 TTFA）。

针对大模型生成的长句流：
1. 首句加速：遇到逗号/顿号/冒号/连词且字数达到门槛（默认 >= 8 字）时立即切分推送，
   使 TTS 引擎在首句前几个词到达时即可开始音频合成；
2. 后续平滑：首句推送后恢复标准句尾标点（。！？；）切分，长句（>= 30 字）遇逗号允许兜底切分。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pipecat.utils.string import SENTENCE_ENDING_PUNCTUATION
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

# 中文弱分句标点（逗号、顿号、冒号、破折号）
WEAK_CLAUSE_PUNCTUATION: frozenset[str] = frozenset({"，", ",", "、", "：", ":", "—", "…", " "})

# 常见连词前置切分词（当句子较长时）
COMMON_CONJUNCTIONS: tuple[str, ...] = (
    "但是",
    "因此",
    "而且",
    "包括",
    "所以",
    "并且",
    "另外",
    "然而",
)


class ChineseClauseTextAggregator(SimpleTextAggregator):
    """支持中文首句弱标点加速与长句自适应切分的文本聚合器。"""

    def __init__(
        self,
        *,
        fast_first_clause: bool = True,
        first_clause_min_chars: int = 8,
        long_clause_max_chars: int = 30,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[no-untyped-call]
        self.fast_first_clause = fast_first_clause
        self.first_clause_min_chars = first_clause_min_chars
        self.long_clause_max_chars = long_clause_max_chars
        self._first_clause_emitted = False

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        if self._aggregation_type == AggregationType.TOKEN:
            if text:
                yield Aggregation(text=text, type=AggregationType.TOKEN)
            return

        for char in text:
            self._text += char
            trimmed = self._text.strip()
            text_len = len(trimmed)

            # 策略 1：首句加速（未发出首句且满足字数门槛）
            if (
                not self._first_clause_emitted
                and self.fast_first_clause
                and text_len >= self.first_clause_min_chars
                and (char in WEAK_CLAUSE_PUNCTUATION or char in SENTENCE_ENDING_PUNCTUATION)
            ):
                result = self._text.strip()
                self._text = ""
                self._needs_lookahead = False
                self._first_clause_emitted = True
                if result:
                    yield Aggregation(text=result, type=AggregationType.SENTENCE)
                continue

            # 策略 2：超长句遇逗号自适应兜底切分
            if (
                self._first_clause_emitted
                and text_len >= self.long_clause_max_chars
                and char in WEAK_CLAUSE_PUNCTUATION
            ):
                result = self._text.strip()
                self._text = ""
                self._needs_lookahead = False
                if result:
                    yield Aggregation(text=result, type=AggregationType.SENTENCE)
                continue

            # 策略 3：标准句尾标点 + lookahead 断句
            result_agg = await self._check_sentence_with_lookahead(char)
            if result_agg:
                self._first_clause_emitted = True
                yield result_agg

    async def flush(self) -> Aggregation | None:
        if self._aggregation_type == AggregationType.TOKEN:
            return None

        if self._text:
            result = self._text.strip()
            await self.reset()
            if result:
                return Aggregation(text=result, type=AggregationType.SENTENCE)
        await self.reset()
        return None

    async def handle_interruption(self) -> None:
        await super().handle_interruption()  # type: ignore[no-untyped-call]
        self._first_clause_emitted = False

    async def reset(self) -> None:
        await super().reset()  # type: ignore[no-untyped-call]
        self._first_clause_emitted = False
