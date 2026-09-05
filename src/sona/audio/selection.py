"""字幕单源采集的公开配置；设备引用不包含 Core Audio UID。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubtitleCaptureSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["microphone", "physical_output"] = "microphone"
    device_ref: str | None = Field(default=None, pattern=r"^vrdev1_[A-Za-z0-9_-]{43}$")

    @model_validator(mode="after")
    def validate_device(self) -> Self:
        if (self.source == "physical_output") != (self.device_ref is not None):
            raise ValueError("physical_output requires a device reference; microphone forbids it")
        return self
