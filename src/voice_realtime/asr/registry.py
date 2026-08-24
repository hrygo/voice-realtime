"""ASR 后端注册表与用途能力门禁。"""

from __future__ import annotations

from collections.abc import Callable

from voice_realtime.asr.contracts import ASRSessionContext, StreamingTranscriber
from voice_realtime.asr.profiles import ASRProfile

StreamingBackendFactory = Callable[[ASRProfile, ASRSessionContext], StreamingTranscriber]


class ASRRegistryError(RuntimeError):
    code = "ASR_REGISTRY_ERROR"


class UnknownBackendError(ASRRegistryError):
    code = "UNKNOWN_ASR_BACKEND"


class DuplicateBackendError(ASRRegistryError):
    code = "DUPLICATE_ASR_BACKEND"


class BackendCapabilityError(ASRRegistryError):
    code = "ASR_CAPABILITY_MISMATCH"


class ASRBackendRegistry:
    """按稳定 ID 构造后端，并在返回前验证用途所需能力。"""

    def __init__(self) -> None:
        self._streaming: dict[str, StreamingBackendFactory] = {}

    def register_streaming(self, backend_id: str, factory: StreamingBackendFactory) -> None:
        normalized = backend_id.strip()
        if not normalized:
            raise ValueError("backend_id 不能为空")
        if normalized in self._streaming:
            raise DuplicateBackendError(f"ASR backend already registered: {normalized}")
        self._streaming[normalized] = factory

    def create_streaming(
        self,
        profile: ASRProfile,
        context: ASRSessionContext,
    ) -> StreamingTranscriber:
        factory = self._streaming.get(profile.kind)
        if factory is None:
            raise UnknownBackendError(f"unknown ASR backend: {profile.kind}")
        backend = factory(profile, context)
        if backend.backend_id != profile.kind:
            raise BackendCapabilityError(
                f"ASR backend identity mismatch: expected {profile.kind}, "
                f"received {backend.backend_id}"
            )
        capabilities = backend.capabilities
        if profile.language not in capabilities.languages:
            raise BackendCapabilityError(
                f"ASR backend {profile.kind} does not support language {profile.language}"
            )
        if context.purpose == "meeting" and not capabilities.supports_segment_timestamps:
            raise BackendCapabilityError(
                f"ASR backend {profile.kind} does not support meeting segment timestamps"
            )
        if context.purpose == "meeting" and not capabilities.supports_eof_flush:
            raise BackendCapabilityError(
                f"ASR backend {profile.kind} does not support meeting EOF flush"
            )
        return backend
