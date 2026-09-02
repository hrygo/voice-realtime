"""音频采集配置及兼容投影。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sona.audio.frame import AudioSourceKind, AudioSourceRole


class CaptureMode(StrEnum):
    """推理前的来源组合方式。"""

    SINGLE = "single"
    DUAL = "dual"


class CaptureSourceSpec(BaseModel):
    """一个采集来源在业务配置中的公开语义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AudioSourceKind
    role: AudioSourceRole


def _default_sources() -> tuple[CaptureSourceSpec, ...]:
    return (
        CaptureSourceSpec(
            kind=AudioSourceKind.MICROPHONE,
            role=AudioSourceRole.NEAR_END,
        ),
    )


class CaptureProfile(BaseModel):
    """严格校验后的采集布局；默认保持 v1 microphone-only。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: CaptureMode = CaptureMode.SINGLE
    follow_default_output: bool = True
    exclude_own_audio: bool = True
    sources: tuple[CaptureSourceSpec, ...] = Field(default_factory=_default_sources)

    @model_validator(mode="after")
    def validate_layout(self) -> Self:
        layout = tuple((source.kind, source.role) for source in self.sources)
        if len(set(layout)) != len(layout):
            raise ValueError("capture profile contains duplicate source layout")

        if self.mode is CaptureMode.SINGLE:
            if len(layout) != 1:
                raise ValueError("single capture requires exactly one source")
            valid_single = {
                (AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END),
                (AudioSourceKind.PHYSICAL_OUTPUT, AudioSourceRole.FAR_END),
            }
            if layout[0] not in valid_single:
                raise ValueError("single capture source kind and role do not match")
            return self

        expected_dual = {
            (AudioSourceKind.MICROPHONE, AudioSourceRole.NEAR_END),
            (AudioSourceKind.PHYSICAL_OUTPUT, AudioSourceRole.FAR_END),
        }
        if len(layout) != 2 or set(layout) != expected_dual:
            raise ValueError(
                "dual capture requires one near-end microphone and one far-end physical output"
            )
        return self

    @classmethod
    def microphone(cls) -> CaptureProfile:
        """构造 v1 兼容的默认麦克风配置。"""
        return cls()

    @property
    def legacy_audio_source(self) -> str:
        """映射到现有会议接口可识别的单值来源。"""
        if self.mode is CaptureMode.DUAL:
            return "mixed"
        return self.sources[0].kind.value
