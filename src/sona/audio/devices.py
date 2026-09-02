"""跨运行入口共享的 PyAudio 输入设备解析。"""

from __future__ import annotations

from typing import Any, Protocol

import pyaudio  # type: ignore[import-untyped]


class AudioInputDeviceError(RuntimeError):
    """麦克风设备选择失败。"""


class _PyAudioDeviceProvider(Protocol):
    def get_default_input_device_info(self) -> dict[str, Any]: ...

    def get_device_count(self) -> int: ...

    def get_device_info_by_index(self, index: int) -> dict[str, Any]: ...


def _require_input_device(provider: _PyAudioDeviceProvider, index: int) -> None:
    try:
        info = provider.get_device_info_by_index(index)
        channels = int(info.get("maxInputChannels", 0) or 0)
    except (OSError, IndexError, TypeError, ValueError) as exc:
        raise AudioInputDeviceError(f"麦克风设备索引无效: {index}") from exc
    if channels <= 0:
        raise AudioInputDeviceError(f"设备索引 {index} 不支持音频输入")


def _input_devices(provider: _PyAudioDeviceProvider) -> list[tuple[int, str]]:
    devices: list[tuple[int, str]] = []
    for index in range(provider.get_device_count()):
        try:
            info = provider.get_device_info_by_index(index)
            channels = int(info.get("maxInputChannels", 0) or 0)
        except (OSError, IndexError, TypeError, ValueError):
            continue
        name = str(info.get("name", "")).strip()
        if channels > 0 and name:
            devices.append((index, name))
    return devices


def resolve_input_device_index_with(
    provider: _PyAudioDeviceProvider,
    *,
    device_index: int | None,
    device_name: str | None,
) -> int:
    """按显式索引、显式名称、系统默认的优先级解析输入设备。"""

    if device_index is not None:
        _require_input_device(provider, device_index)
        return device_index
    if device_name is None:
        try:
            default_index = int(provider.get_default_input_device_info()["index"])
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise AudioInputDeviceError("无法获取系统默认输入设备") from exc
        _require_input_device(provider, default_index)
        return default_index

    selector = device_name.casefold()
    devices = _input_devices(provider)
    exact = [device for device in devices if device[1].casefold() == selector]
    if len(exact) == 1:
        return exact[0][0]
    fragments = [device for device in devices if selector in device[1].casefold()]
    if len(fragments) == 1:
        return fragments[0][0]
    if len(fragments) > 1:
        names = ", ".join(name for _, name in fragments)
        raise AudioInputDeviceError(
            f"麦克风名称 {device_name!r} 匹配到多个输入设备: {names}"
        )
    available = ", ".join(name for _, name in devices) or "无"
    raise AudioInputDeviceError(
        f"未找到输入设备 {device_name!r}；可用输入设备: {available}"
    )


def resolve_input_device_index(
    *,
    device_index: int | None,
    device_name: str | None,
) -> int:
    """创建短生命周期 PyAudio 实例，为 headless transport 解析设备索引。"""

    provider = pyaudio.PyAudio()
    try:
        return resolve_input_device_index_with(
            provider,
            device_index=device_index,
            device_name=device_name,
        )
    finally:
        provider.terminate()
