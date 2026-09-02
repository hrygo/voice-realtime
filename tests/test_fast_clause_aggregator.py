"""测试中文流式首句弱标点极速分词聚合器（ChineseClauseTextAggregator）。"""

import pytest
from pipecat.utils.text.base_text_aggregator import AggregationType

from sona.interaction.fast_clause_aggregator import ChineseClauseTextAggregator


@pytest.mark.asyncio
async def test_fast_first_clause_splitting_on_comma() -> None:
    aggregator = ChineseClauseTextAggregator(
        fast_first_clause=True,
        first_clause_min_chars=8,
    )

    # 模拟流式传入："今天我们讨论三个方面，首先是系统架构设计。"
    # 期望："今天我们讨论三个方面，" 作为第一句直接切出（字数 >= 8 且遇逗号）
    stream = "今天我们讨论三个方面，首先是系统架构设计。"
    chunks = [agg.text async for agg in aggregator.aggregate(stream)]

    # flush 剩余部分
    remaining = await aggregator.flush()
    if remaining:
        chunks.append(remaining.text)

    assert len(chunks) == 2
    assert chunks[0] == "今天我们讨论三个方面，"
    assert "首先是系统架构设计。" in chunks[1]


@pytest.mark.asyncio
async def test_fast_first_clause_min_chars_guard() -> None:
    aggregator = ChineseClauseTextAggregator(
        fast_first_clause=True,
        first_clause_min_chars=8,
    )

    # 短词遇逗号（如"好的，"共 3 字 < 8 字）不应过早切分
    stream = "好的，请问今天开会的主要议题是什么？"
    chunks = [agg.text async for agg in aggregator.aggregate(stream)]

    remaining = await aggregator.flush()
    if remaining:
        chunks.append(remaining.text)

    # 因为"好的，"只有 3 字，不会在"好的，"处切分，而是在整句问号处完成
    assert len(chunks) == 1
    assert "好的，请问今天开会的主要议题是什么？" in chunks[0]


@pytest.mark.asyncio
async def test_fast_first_clause_disabled() -> None:
    aggregator = ChineseClauseTextAggregator(
        fast_first_clause=False,
    )

    stream = "今天我们讨论三个方面，首先是系统架构设计。"
    chunks = [agg.text async for agg in aggregator.aggregate(stream)]

    remaining = await aggregator.flush()
    if remaining:
        chunks.append(remaining.text)

    # 关闭首句加速时，逗号不切分，在句号处完整切出
    assert len(chunks) == 1
    assert chunks[0] == "今天我们讨论三个方面，首先是系统架构设计。"


@pytest.mark.asyncio
async def test_long_clause_fallback_split() -> None:
    aggregator = ChineseClauseTextAggregator(
        fast_first_clause=True,
        first_clause_min_chars=8,
        long_clause_max_chars=25,
    )

    # 第一句正常切出后，第二句如果特别长（>= 25字）且没有句号只有逗号，会在逗号处兜底切分
    stream = "首句已经发出了。这是一段非常非常长的后续说明文本，中间包含了多个连续的从句说明。"
    chunks = [agg.text async for agg in aggregator.aggregate(stream)]

    remaining = await aggregator.flush()
    if remaining:
        chunks.append(remaining.text)

    assert len(chunks) >= 2


@pytest.mark.asyncio
async def test_interruption_resets_state() -> None:
    aggregator = ChineseClauseTextAggregator(fast_first_clause=True, first_clause_min_chars=8)

    # 传入半句话
    async for _ in aggregator.aggregate("这是刚刚说了一半的话"):
        pass

    # 发生打断
    await aggregator.handle_interruption()
    assert aggregator._text == ""
    assert aggregator._first_clause_emitted is False


@pytest.mark.asyncio
async def test_token_mode_passthrough() -> None:
    aggregator = ChineseClauseTextAggregator(
        aggregation_type=AggregationType.TOKEN,
    )

    chunks = [agg.text async for agg in aggregator.aggregate("Hello world")]

    assert chunks == ["Hello world"]
    assert await aggregator.flush() is None
