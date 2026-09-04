"""SpeechRail 非流式分人转写客户端。

会议助手在流式 ASR 之外，为了拿到**权威、词级、带说话人标签**的分人结果，
需要把缓冲 PCM 提交给 SpeechRail ``POST /v1/audio/transcriptions`` 的
diarize 端点（``model=gpt-4o-transcribe-diarize`` + ``verbose_json`` +
``timestamp_granularities=segment``）。本模块只负责：

1. 把 16 kHz mono s16le PCM 封装为 WAV 并做 multipart 上传；
2. 解析响应中的 ``segments``（词级时间戳 + 匿名 speaker 标签）；
3. 把异常映射为稳定的 :class:`SpeechRailProtocolError`（复用 transport 错误契）。

分人后的 speaker 归属如何回流到会议转录，属 :mod:`sona.meeting.diarization_overlay`
职责；这里保持纯粹的 SpeechRail 基础设施客户端边界。
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from sona.speechrail.transport import SpeechRailProtocolError

_DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
_WAV_HEADER_MAX_PCM_BYTES = 120_000_000  # 防止超大 WAV 头溢出（约 62 分钟 16kHz PCM）


@dataclass(frozen=True, slots=True)
class SpeechRailDiarizeSegment:
    """词级分人结果：文本 + 时间戳 + 匿名说话人标签。"""

    text: str
    start_ms: int
    end_ms: int
    speaker: str | None

    @property
    def has_speaker(self) -> bool:
        return bool(self.speaker)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class SpeechRailDiarizeResult:
    """一次非流式 diarize 的稳定产出。"""

    text: str
    language: str | None
    segments: tuple[SpeechRailDiarizeSegment, ...]

    @property
    def has_segments(self) -> bool:
        return bool(self.segments)


def _http_base_url(ws_url: str) -> str:
    """把 ``ws(s)://host/v1/realtime`` 归一化为 ``http(s)://host``。

    无法从 ws URL 推导时回退到原 URL 原名（下游请求会 404 并映射为错误）。
    """
    parsed = urlsplit(ws_url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    if not parsed.hostname:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{parsed.hostname}:{parsed.port}"
    return f"{scheme}://{netloc}"


def _to_wav(pcm: bytes) -> bytes:
    """把 16 kHz mono s16le PCM 封装进 16-bit WAV 容器。

    SpeechRail 的 ``/v1/audio/transcriptions`` 接受原始 WAV；这里不重采样，
    只补 WAV 头。要求输入已经是 16 kHz mono s16le。
    """
    if not pcm or len(pcm) % 2:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    if len(pcm) > _WAV_HEADER_MAX_PCM_BYTES:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)  # SpeechRail meeting PCM 固定 16kHz
        wav.writeframes(pcm)
    return buffer.getvalue()


class SpeechRailBatchTranscriber:
    """SpeechRail 非流式 diarize 转写客户端。

    传入 ``url``（可用 WS /v1/realtime 地址，内部推导 http 基址）与可选
    ``api_key``；``http_client`` 仅供测试注入
    （用 :class:`httpx.MockTransport` 隔离网络）。
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        language: str = "Chinese",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        base_url = _http_base_url(url)
        self._base_url = base_url
        self._api_key = api_key
        self._language = language.strip() or "Chinese"
        self._http_client = http_client
        self._owns_client = http_client is None

    async def transcribe_diarize(self, pcm: bytes) -> SpeechRailDiarizeResult:
        """对一段 16 kHz mono s16le PCM 执行非流式分人转写。"""
        if not pcm or len(pcm) % 2:
            raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
        wav = _to_wav(pcm)
        client = self._http_client or httpx.AsyncClient(timeout=90.0)
        try:
            response = await client.post(
                f"{self._base_url}/v1/audio/transcriptions",
                data={
                    "model": _DIARIZE_MODEL,
                    "language": self._language,
                    "response_format": "verbose_json",
                    "timestamp_granularities": "segment",
                },
                files={"file": ("meeting.wav", wav, "audio/wav")},
                headers=(
                    {"Authorization": f"Bearer {self._api_key}"}
                    if self._api_key
                    else None
                ),
            )
        except httpx.HTTPError as exc:
            raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR") from exc
        finally:
            if self._owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
        return _parse_diarize_response(response.content)


def _parse_diarize_response(payload: bytes) -> SpeechRailDiarizeResult:
    import json

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR") from exc
    if not isinstance(data, dict):
        raise SpeechRailProtocolError("SPEECHRAIL_AUDIO_ERROR")
    text = data.get("text")
    if not isinstance(text, str):
        text = ""
    language = data.get("language")
    if not isinstance(language, str) or not language.strip():
        language = None
    raw_segments = data.get("segments")
    segments = _parse_segments(raw_segments)
    return SpeechRailDiarizeResult(text=text, language=language, segments=segments)


def _parse_segments(raw_segments: object) -> tuple[SpeechRailDiarizeSegment, ...]:
    if not isinstance(raw_segments, list):
        return ()
    result: list[SpeechRailDiarizeSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        seg_text = raw.get("text")
        if not isinstance(seg_text, str) or not seg_text.strip():
            continue
        start = raw.get("start")
        end = raw.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or start < 0
            or end < start
        ):
            continue
        speaker = raw.get("speaker")
        result.append(
            SpeechRailDiarizeSegment(
                text=seg_text.strip(),
                start_ms=round(start * 1000),
                end_ms=round(end * 1000),
                speaker=(speaker if isinstance(speaker, str) and speaker.strip() else None),
            )
        )
    return tuple(result)


__all__ = [
    "SpeechRailBatchTranscriber",
    "SpeechRailDiarizeResult",
    "SpeechRailDiarizeSegment",
]
