"""只读设备列表；显式枚举不创建 Tap，也不请求录制权限。"""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sona.audio.output_source import AudioCaptureError, HelperSupervisor
from sona.config import AudioCaptureSettings


class OutputDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    device_ref: str = Field(pattern=r"^vrdev1_[A-Za-z0-9_-]{43}$")
    label: str = Field(min_length=1, max_length=128)
    transport: Literal[
        "built_in", "bluetooth", "usb", "hdmi", "display", "airplay", "virtual", "other"
    ]
    is_default: bool


class OutputDevicesResponse(BaseModel):
    enabled: bool
    devices: list[OutputDevice]


def create_audio_device_router(settings: AudioCaptureSettings) -> APIRouter:
    router = APIRouter()
    lock = asyncio.Lock()

    @router.get("/api/audio/output-devices", response_model=OutputDevicesResponse)
    async def output_devices() -> OutputDevicesResponse:
        if not settings.enabled:
            return OutputDevicesResponse(enabled=False, devices=[])
        async with lock:
            supervisor = HelperSupervisor(settings)
            try:
                client = await supervisor.start_client()
                devices = await client.list_devices()
                if len(devices) > 128:
                    raise ValueError("too many devices")
                return OutputDevicesResponse(
                    enabled=True, devices=[OutputDevice.model_validate(item) for item in devices]
                )
            except AudioCaptureError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (ValidationError, ValueError) as exc:
                raise HTTPException(status_code=502, detail="输出设备列表无效") from exc
            finally:
                await supervisor.stop()

    return router
