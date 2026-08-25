"""AliMeeting long TextGrid 解析与 speaker-turn 候选测试。"""

from __future__ import annotations

import pytest

from voice_realtime.benchmarks.asr.public_proxy_sources import (
    NonSpeechCandidate,
    SpeakerTurnCandidate,
    generate_non_speech_candidates,
    generate_speaker_turn_candidates,
    normalize_aishell4_reference,
    parse_long_textgrid,
)


def _textgrid(*items: str, xmax: str = "2") -> str:
    body = "\n".join(items)
    return f'''File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = {xmax}
tiers? <exists>
size = {len(items)}
item []:
{body}
'''


def _tier(name: str, *intervals: tuple[str, str, str]) -> str:
    rows = "\n".join(
        f'''        intervals [{index}]:
            xmin = {start}
            xmax = {end}
            text = "{text}"'''
        for index, (start, end, text) in enumerate(intervals, start=1)
    )
    return f'''    item [1]:
        class = "IntervalTier"
        name = "{name}"
        xmin = 0
        xmax = 2
        intervals: size = {len(intervals)}
{rows}'''


def _two_tiers(first: str, second: str) -> str:
    # Replace the second item's index while keeping the fixture readable.
    return _textgrid(first, second.replace("item [1]:", "item [2]:"))


def test_parser_decodes_escaped_quotes_and_retains_empty_metadata() -> None:
    parsed = parse_long_textgrid(
        _textgrid(
            _tier(
                "spk1",
                ("0", "0.001", r'He said \"hi\"'),
                ("0.001", "0.002", ""),
            )
        ),
        session="eval-001",
        content_group="eval-001-far",
    )

    assert len(parsed.intervals) == 2
    assert parsed.intervals[0].reference == 'He said "hi"'
    assert parsed.intervals[1].reference == ""
    assert parsed.intervals[1].is_empty is True
    assert generate_speaker_turn_candidates(parsed) == (
        SpeakerTurnCandidate(
            candidate_id=parsed.intervals[0].candidate_id(
                session="eval-001",
                content_group="eval-001-far",
            ),
            session="eval-001",
            content_group="eval-001-far",
            speaker="spk1",
            start_frame=0,
            end_frame=16,
            duration_ms=1,
            reference='He said "hi"',
        ),
    )


def test_half_open_boundaries_do_not_create_false_overlap() -> None:
    parsed = parse_long_textgrid(
        _two_tiers(
            _tier("spk1", ("0", "1", "left")),
            _tier("spk2", ("1", "2", "right")),
        ),
        session="eval-001",
        content_group="eval-001-near",
    )

    assert parsed.overlaps == ()
    candidates = generate_speaker_turn_candidates(parsed)
    assert [(item.speaker, item.start_frame, item.end_frame) for item in candidates] == [
        ("spk1", 0, 16_000),
        ("spk2", 16_000, 32_000),
    ]


def test_cross_speaker_overlap_is_reported_and_overlapping_turns_are_excluded() -> None:
    parsed = parse_long_textgrid(
        _two_tiers(
            _tier("spk1", ("0", "1", "first")),
            _tier("spk2", ("0.5", "1.5", "overlap"), ("1.5", "2", "tail")),
        ),
        session="eval-002",
        content_group="eval-002-far",
    )

    assert len(parsed.overlaps) == 1
    overlap = parsed.overlaps[0]
    assert (overlap.speaker_a, overlap.speaker_b) == ("spk1", "spk2")
    assert (overlap.start_frame, overlap.end_frame) == (8_000, 16_000)
    candidates = generate_speaker_turn_candidates(parsed)
    assert [(item.speaker, item.reference) for item in candidates] == [("spk2", "tail")]


def test_non_speech_candidates_are_real_all_speaker_gaps_and_split_deterministically() -> None:
    parsed = parse_long_textgrid(
        _two_tiers(
            _tier("spk1", ("0", "1", "left"), ("1", "6", "")),
            _tier("spk2", ("0", "4", ""), ("4", "5", "right"), ("5", "6", "")),
        ),
        session="eval-negative",
        content_group="eval-negative-far",
    )

    assert [item.reference for item in generate_speaker_turn_candidates(parsed)] == [
        "left",
        "right",
    ]

    candidates = generate_non_speech_candidates(
        parsed,
        min_duration_ms=500,
        max_duration_ms=1_500,
    )

    assert candidates == (
        NonSpeechCandidate(
            candidate_id=candidates[0].candidate_id,
            session="eval-negative",
            content_group="eval-negative-far",
            start_frame=16_000,
            end_frame=40_000,
            duration_ms=1_500,
        ),
        NonSpeechCandidate(
            candidate_id=candidates[1].candidate_id,
            session="eval-negative",
            content_group="eval-negative-far",
            start_frame=40_000,
            end_frame=64_000,
            duration_ms=1_500,
        ),
        NonSpeechCandidate(
            candidate_id=candidates[2].candidate_id,
            session="eval-negative",
            content_group="eval-negative-far",
            start_frame=80_000,
            end_frame=96_000,
            duration_ms=1_000,
        ),
    )


def test_non_speech_chunks_never_exceed_maximum_when_remainder_is_short() -> None:
    parsed = parse_long_textgrid(
        _tier_textgrid(("0", "1", "speech"), ("1", "2.8", "")),
        session="eval-negative-remainder",
        content_group="eval-negative-remainder-far",
    )

    candidates = generate_non_speech_candidates(
        parsed,
        min_duration_ms=500,
        max_duration_ms=1_500,
    )

    assert [item.duration_ms for item in candidates] == [1_300, 500]


def test_non_lexical_noise_markers_are_not_positive_transcripts() -> None:
    parsed = parse_long_textgrid(
        _tier_textgrid(
            ("0", "1", "<%>"),
            ("1", "2", "<$>"),
            ("2", "3", "lexical"),
        ),
        session="eval-markers",
        content_group="eval-markers-far",
    )

    assert [item.reference for item in generate_speaker_turn_candidates(parsed)] == [
        "lexical"
    ]
    assert [item.duration_ms for item in generate_non_speech_candidates(parsed)] == [
        2_000
    ]


def test_aishell_boundary_policy_quantizes_to_nearest_millisecond_explicitly() -> None:
    text = _tier_textgrid(("0", "0.00129", "short"))

    with pytest.raises(ValueError, match="sample frame"):
        parse_long_textgrid(
            text,
            session="aishell-strict",
            content_group="aishell-strict-far",
        )

    parsed = parse_long_textgrid(
        text,
        session="aishell-quantized",
        content_group="aishell-quantized-far",
        boundary_policy="nearest-ms",
    )

    assert (parsed.intervals[0].start_frame, parsed.intervals[0].end_frame) == (0, 16)


def test_aishell_boundary_policy_drops_only_intervals_collapsed_by_quantization() -> None:
    parsed = parse_long_textgrid(
        _tier_textgrid(
            ("0", "0.0004", ""),
            ("0.0004", "0.0014", "word"),
        ),
        session="aishell-collapsed",
        content_group="aishell-collapsed-far",
        boundary_policy="nearest-ms",
    )

    assert [(item.start_frame, item.end_frame, item.reference) for item in parsed.intervals] == [
        (0, 16, "word")
    ]
    assert parsed.collapsed_interval_count == 1


def test_aishell_reference_normalization_removes_only_annotation_controls() -> None:
    assert normalize_aishell4_reference(
        "今天<sil>&开会&<#><-><$><%><_><space>`，保留标点"
    ) == "今天开会，保留标点"


def test_candidates_require_integer_millisecond_duration_and_never_trim() -> None:
    parsed = parse_long_textgrid(
        _tier_textgrid(
            ("0", "0.0005", "half-ms"),
            ("0.001", "0.002", "one-ms"),
        ),
        session="eval-003",
        content_group="eval-003-near",
    )

    candidates = generate_speaker_turn_candidates(parsed)
    assert [item.reference for item in candidates] == ["one-ms"]
    assert candidates[0].start_frame == 16
    assert candidates[0].end_frame == 32
    assert candidates[0].duration_ms == 1


def _tier_textgrid(*intervals: tuple[str, str, str]) -> str:
    return _textgrid(_tier("spk1", *intervals))


def test_candidate_order_and_ids_are_deterministic() -> None:
    first = parse_long_textgrid(
        _two_tiers(
            _tier("spk2", ("1", "2", "b")),
            _tier("spk1", ("0", "1", "a")),
        ),
        session="eval-004",
        content_group="eval-004-far",
    )
    second = parse_long_textgrid(
        _two_tiers(
            _tier("spk1", ("0", "1", "a")),
            _tier("spk2", ("1", "2", "b")),
        ),
        session="eval-004",
        content_group="eval-004-far",
    )

    first_candidates = generate_speaker_turn_candidates(first)
    second_candidates = generate_speaker_turn_candidates(second)
    assert first_candidates == second_candidates
    assert [item.speaker for item in first_candidates] == ["spk1", "spk2"]


@pytest.mark.parametrize(
    "text",
    [
        _textgrid('''    item [1]:
        class = "IntervalTier"
        name = "spk1"
        xmin = 0
        xmax = 1
        intervals: size = 1
        intervals [1]:
            xmin = 0
            xmax = 1'''),
        _textgrid('''    item [1]:
        class = "IntervalTier"
        name = "spk1"
        xmin = 0
        xmax = 1
        intervals: size = 1
        intervals [1]:
            xmin = 1
            xmax = 0
            text = "bad"'''),
        "Object class = \"Table\"\n",
    ],
)
def test_parser_rejects_malformed_long_textgrid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_long_textgrid(text, session="eval-005", content_group="eval-005-far")


def test_parser_rejects_non_integral_sample_boundary_and_unsafe_identity() -> None:
    with pytest.raises(ValueError, match="sample frame"):
        parse_long_textgrid(
            _tier_textgrid(("0", "0.000001", "not-frame-aligned")),
            session="eval-006",
            content_group="eval-006-far",
        )

    with pytest.raises(ValueError, match="identity"):
        parse_long_textgrid(
            _tier_textgrid(("0", "1", "text")),
            session="../private/session",
            content_group="eval-006-far",
        )
