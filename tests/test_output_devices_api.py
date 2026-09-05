from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from sona.config import AudioCaptureSettings
from sona.ui.audio_devices import create_audio_device_router


async def request_devices(*, enabled, devices):
    app = FastAPI()
    settings = AudioCaptureSettings(_env_file=None, enabled=enabled)
    app.include_router(create_audio_device_router(settings))
    supervisor = AsyncMock()
    supervisor.start_client.return_value.list_devices.return_value = devices
    with patch("sona.ui.audio_devices.HelperSupervisor", return_value=supervisor) as factory:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/audio/output-devices")
    return response, supervisor, factory


async def test_disabled_device_api_does_not_start_helper():
    response, _, factory = await request_devices(enabled=False, devices=[])
    assert response.json() == {"enabled": False, "devices": []}
    factory.assert_not_called()


async def test_device_api_lists_without_preparing_capture_and_releases_helper():
    devices = [{"device_ref": "vrdev1_" + "A" * 43, "label": "测试扬声器",
                "transport": "built_in", "is_default": True}]
    response, supervisor, _ = await request_devices(enabled=True, devices=devices)
    assert response.status_code == 200
    assert response.json()["devices"] == devices
    supervisor.start_client.return_value.prepare_capture.assert_not_called()
    supervisor.stop.assert_awaited_once()


async def test_device_api_rejects_private_fields_without_echoing_them():
    response, supervisor, _ = await request_devices(
        enabled=True, devices=[{"uid": "private-device"}]
    )
    assert response.status_code == 502
    assert "private-device" not in response.text
    supervisor.stop.assert_awaited_once()
