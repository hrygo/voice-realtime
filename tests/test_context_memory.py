"""结构化对话记忆、滚动窗口与压缩策略契约测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from sona.interaction.context_memory import (
    CompactionDecision,
    ContextCompactionConfig,
    ConversationMemoryPacket,
    ConversationMemorySnapshot,
    ConversationTurn,
    build_compaction_window,
    build_memory_packet,
    empty_memory_snapshot,
    fit_compaction_window,
    normalize_completed_turns,
    parse_snapshot,
    should_compact,
)


def valid_snapshot_payload(source_turn_start: int, source_turn_end: int) -> dict[str, Any]:
    source_ids = [source_turn_end] if source_turn_end else []
    entities: list[dict[str, Any]] = []
    if source_ids:
        entities.append(
            {
                "id": "project_voice",
                "type": "project",
                "name": "sona",
                "aliases": ["声流"],
                "facts": [
                    {
                        "value": "使用本地 LM Studio",
                        "status": "active",
                        "source_turn_ids": source_ids,
                    }
                ],
            }
        )
    return {
        "schema_version": 1,
        "source_turn_start": source_turn_start,
        "source_turn_end": source_turn_end,
        "participants": [
            {"id": "local_user", "role": "user", "names": ["用户"]},
            {"id": "voice_assistant", "role": "assistant", "names": ["助手"]},
        ],
        "entities": entities,
        "user_preferences": [],
        "goals_and_constraints": [],
        "decisions": [],
        "open_items": [],
        "conversation_summary": "用户正在设计上下文压缩。" if source_ids else "",
    }


def alternating_turns(count: int, *, content_size: int = 0) -> list[ConversationTurn]:
    return [
        ConversationTurn(
            turn_id=index,
            role="user" if index % 2 else "assistant",
            content=(f"第{index}条消息" + "内容" * content_size),
        )
        for index in range(1, count + 1)
    ]


def test_empty_snapshot_has_fixed_participants_and_zero_source_range() -> None:
    snapshot = empty_memory_snapshot()

    assert snapshot.source_turn_start == 0
    assert snapshot.source_turn_end == 0
    assert [(item.id, item.role) for item in snapshot.participants] == [
        ("local_user", "user"),
        ("voice_assistant", "assistant"),
    ]
    assert snapshot.entities == []


def test_snapshot_rejects_extra_fields_and_out_of_range_sources() -> None:
    payload = valid_snapshot_payload(1, 4)
    payload["unexpected"] = "拒绝"
    with pytest.raises(ValidationError):
        ConversationMemorySnapshot.model_validate(payload)

    payload = valid_snapshot_payload(1, 4)
    payload["entities"][0]["facts"][0]["source_turn_ids"] = [5]
    with pytest.raises(ValidationError, match="source_turn_ids"):
        ConversationMemorySnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (1, 0), (4, 3), (-1, 0)],
)
def test_snapshot_rejects_invalid_source_ranges(start: int, end: int) -> None:
    with pytest.raises(ValidationError):
        ConversationMemorySnapshot.model_validate(valid_snapshot_payload(start, end))


def test_snapshot_requires_fixed_participant_role_mapping() -> None:
    payload = valid_snapshot_payload(1, 2)
    payload["participants"][0]["role"] = "assistant"

    with pytest.raises(ValidationError, match="participant"):
        ConversationMemorySnapshot.model_validate(payload)


def test_parse_snapshot_requires_exact_expected_range_and_json_only() -> None:
    raw = json.dumps(valid_snapshot_payload(1, 4), ensure_ascii=False)
    snapshot = parse_snapshot(raw, expected_start=1, expected_end=4)
    assert snapshot.source_turn_end == 4

    with pytest.raises(ValueError, match="source range"):
        parse_snapshot(raw, expected_start=1, expected_end=2)
    with pytest.raises(ValueError, match="JSON"):
        parse_snapshot(f"```json\n{raw}\n```", expected_start=1, expected_end=4)


def test_normalize_completed_turns_preserves_roles_and_drops_trailing_user() -> None:
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "尚未回答"},
    ]

    turns = normalize_completed_turns(messages)

    assert [(turn.turn_id, turn.role, turn.content) for turn in turns] == [
        (1, "user", "问题一"),
        (2, "assistant", "回答一"),
    ]


def test_normalize_completed_turns_appends_committed_assistant_text() -> None:
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "问题一"},
    ]

    turns = normalize_completed_turns(messages, assistant_text="回答一")

    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "问题一"),
        ("assistant", "回答一"),
    ]


def test_normalize_completed_turns_keeps_latest_consecutive_user_after_interruption() -> None:
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "被打断的问题"},
        {"role": "user", "content": "打断后的新问题"},
        {"role": "assistant", "content": "新问题的回答"},
        {"role": "user", "content": "尚未回答"},
    ]

    turns = normalize_completed_turns(messages)

    assert [(turn.turn_id, turn.role, turn.content) for turn in turns] == [
        (1, "user", "打断后的新问题"),
        (2, "assistant", "新问题的回答"),
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "缺少系统"}],
        [
            {"role": "system", "content": "系统"},
            {"role": "assistant", "content": "角色顺序错误"},
        ],
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": []},
        ],
        [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
            {"role": "assistant", "content": "重复回答"},
        ],
    ],
)
def test_normalize_completed_turns_rejects_invalid_role_history(
    messages: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError):
        normalize_completed_turns(messages)  # type: ignore[arg-type]


def test_window_rolls_forward_and_keeps_four_recent_pairs() -> None:
    window = build_compaction_window(
        alternating_turns(12),
        empty_memory_snapshot(),
        recent_turn_pairs=4,
    )

    assert window is not None
    assert [turn.turn_id for turn in window.turns_to_summarize] == [1, 2, 3, 4]
    assert [turn.turn_id for turn in window.recent_turns] == list(range(5, 13))
    assert window.expected_snapshot_start == 1
    assert window.expected_snapshot_end == 4


def test_window_uses_previous_snapshot_and_only_new_complete_turns() -> None:
    previous = ConversationMemorySnapshot.model_validate(valid_snapshot_payload(1, 4))

    window = build_compaction_window(
        alternating_turns(16),
        previous,
        recent_turn_pairs=4,
    )

    assert window is not None
    assert [turn.turn_id for turn in window.turns_to_summarize] == [5, 6, 7, 8]
    assert [turn.turn_id for turn in window.recent_turns] == list(range(9, 17))
    assert window.expected_snapshot_start == 1
    assert window.expected_snapshot_end == 8


def test_window_returns_none_when_only_recent_pairs_exist() -> None:
    assert (
        build_compaction_window(
            alternating_turns(8),
            empty_memory_snapshot(),
            recent_turn_pairs=4,
        )
        is None
    )


def test_default_window_keeps_sixteen_recent_pairs() -> None:
    config = ContextCompactionConfig()

    window = build_compaction_window(
        alternating_turns(40),
        empty_memory_snapshot(),
        recent_turn_pairs=config.recent_turn_pairs,
    )

    assert window is not None
    assert [turn.turn_id for turn in window.turns_to_summarize] == list(range(1, 9))
    assert [turn.turn_id for turn in window.recent_turns] == list(range(9, 41))


def test_packet_rejects_non_alternating_or_non_contiguous_recent_turns() -> None:
    snapshot = ConversationMemorySnapshot.model_validate(valid_snapshot_payload(1, 2))
    same_role = [
        ConversationTurn(turn_id=3, role="user", content="问题一"),
        ConversationTurn(turn_id=4, role="user", content="问题二"),
    ]
    with pytest.raises(ValueError, match="alternate"):
        build_memory_packet(snapshot, same_role)

    non_contiguous = [
        ConversationTurn(turn_id=3, role="user", content="问题"),
        ConversationTurn(turn_id=5, role="assistant", content="回答"),
    ]
    with pytest.raises(ValueError, match="contiguous"):
        build_memory_packet(snapshot, non_contiguous)


def test_fit_window_moves_oldest_complete_pairs_into_summary() -> None:
    turns = alternating_turns(12, content_size=40)
    window = build_compaction_window(turns, empty_memory_snapshot(), recent_turn_pairs=4)
    assert window is not None
    recent_bytes = len(
        json.dumps(
            [turn.model_dump() for turn in window.recent_turns],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    fitted = fit_compaction_window(window, max_recent_bytes=recent_bytes // 2)

    assert 2 <= len(fitted.recent_turns) < len(window.recent_turns)
    assert len(fitted.recent_turns) % 2 == 0
    assert fitted.recent_turns[-2:] == tuple(turns[-2:])
    assert fitted.turns_to_summarize[-1].turn_id + 1 == fitted.recent_turns[0].turn_id
    assert fitted.expected_snapshot_end == fitted.turns_to_summarize[-1].turn_id


def test_fit_window_never_truncates_latest_pair() -> None:
    window = build_compaction_window(
        alternating_turns(4, content_size=200),
        empty_memory_snapshot(),
        recent_turn_pairs=1,
    )
    assert window is not None
    fitted = fit_compaction_window(window, max_recent_bytes=64)
    assert len(fitted.recent_turns) == 2
    assert fitted.recent_turns[-1].role == "assistant"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"enabled": False, "input_tokens": 200000}, CompactionDecision(False, "disabled")),
        (
            {"input_tokens": 210000, "model_context_length": 262144},
            CompactionDecision(True, "capacity"),
        ),
        ({"input_tokens": 32768}, CompactionDecision(True, "hard")),
        ({"input_tokens": 16384}, CompactionDecision(True, "tokens")),
        ({"unsummarized_messages": 128}, CompactionDecision(True, "messages")),
        (
            {"ttft_seconds": 3.1, "ttft_soft_hits": 2},
            CompactionDecision(True, "ttft"),
        ),
        ({"input_tokens": 16383}, CompactionDecision(False, "none")),
    ],
)
def test_policy_uses_real_usage_with_deterministic_precedence(
    kwargs: dict[str, Any], expected: CompactionDecision
) -> None:
    config = ContextCompactionConfig(enabled=kwargs.pop("enabled", True))
    actual = should_compact(
        config,
        input_tokens=kwargs.pop("input_tokens", 1000),
        ttft_seconds=kwargs.pop("ttft_seconds", 0.2),
        ttft_soft_hits=kwargs.pop("ttft_soft_hits", 0),
        unsummarized_messages=kwargs.pop("unsummarized_messages", 2),
        model_context_length=kwargs.pop("model_context_length", None),
    )
    assert kwargs == {}
    assert actual == expected


def test_context_compaction_config_rejects_invalid_threshold_order() -> None:
    with pytest.raises(ValueError, match=r"target.*soft.*hard"):
        ContextCompactionConfig(
            target_input_tokens=16384,
            soft_input_tokens=16384,
            hard_input_tokens=32768,
        )


def test_memory_packet_is_strict_model() -> None:
    payload = build_memory_packet(empty_memory_snapshot(), alternating_turns(2)).model_dump()
    payload["unknown"] = True
    with pytest.raises(ValidationError):
        ConversationMemoryPacket.model_validate(payload)
