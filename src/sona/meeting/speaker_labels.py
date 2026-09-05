"""Public display labels for opaque meeting speaker identities."""

from __future__ import annotations

import re

__all__ = ["UNKNOWN_SPEAKER_LABEL", "speaker_display_label"]

UNKNOWN_SPEAKER_LABEL = "未识别说话人"

_NUMERIC_SPEAKER = re.compile(r"^(?:spk_|s)?0*([1-9][0-9]*)$")


def speaker_display_label(
    speaker_key: object,
    raw_speaker: object | None = None,
) -> str:
    """Convert an opaque speaker identity to a safe, stable display label.

    SpeechRail's ``spk_01`` and legacy ``s1`` forms are numeric identities;
    zero and all non-numeric forms stay anonymous instead of exposing the
    epoch/group identity or guessing a person's name.
    """
    candidates: list[str] = []
    for value in (raw_speaker, speaker_key):
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        candidates.append(text.rsplit(":", 1)[-1])

    for candidate in candidates:
        match = _NUMERIC_SPEAKER.fullmatch(candidate)
        if match is not None:
            return f"说话人 {int(match.group(1))}"
    return UNKNOWN_SPEAKER_LABEL
