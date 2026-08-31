"""用于前后端分离联调的会议助手契约 mock 服务。

该服务只读取 ``contracts/meeting-assistant/v1/fixtures``，不连接 PostgreSQL、
不加载 ASR/TTS 模型，也不保存用户数据。它提供与真实会议服务相同的主要
HTTP/WS 入口，并通过查询参数回放可重复的重连、revision gap 和断线场景。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response

CONTRACT_VERSION = "1"
_DEFAULT_MEETING_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SECOND_MEETING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_FIXTURE_FILES = {
    "snapshot": "meeting-snapshot-active.json",
    "partial": "transcript-partial.json",
    "reconciled": "transcript-reconciled.json",
    "speaker": "speaker-updated.json",
    "health": "health-changed.json",
    "gap": "transcription-gap.json",
    "resync": "resync-required.json",
    "revision_gap": "revision-gap.json",
    "finalizing": "finalizing.json",
    "completed": "completed.json",
    "minutes": "minutes-completed.json",
}


def _default_fixture_dir() -> Path:
    package_root = Path(__file__).resolve().parents[3]
    candidates = (
        package_root / "contracts/meeting-assistant/v1/fixtures",
        Path.cwd() / "contracts/meeting-assistant/v1/fixtures",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取会议契约 fixture: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"会议契约 fixture 必须是 object: {path}")
    return value


def _copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(value)
    if not isinstance(copied, dict):
        raise RuntimeError("fixture copy must remain an object")
    return copied


def _error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id[:128],
                "details": {},
            }
        },
    )


class MeetingFixtureStore:
    """内存中的可变 fixture 状态；每次启动 mock 服务时重新初始化。"""

    def __init__(self, fixture_dir: Path | None = None) -> None:
        root = fixture_dir or _default_fixture_dir()
        self.fixture_dir = root.expanduser().resolve()
        self.events = {
            name: _read_json(self.fixture_dir / filename)
            for name, filename in _FIXTURE_FILES.items()
        }
        snapshot = self.events["snapshot"]
        self.meeting_id = str(snapshot.get("meeting_id") or _DEFAULT_MEETING_ID)
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("meeting-snapshot-active fixture payload 无效")
        meeting = payload.get("meeting")
        if not isinstance(meeting, dict):
            raise RuntimeError("meeting-snapshot-active fixture meeting 无效")
        self._meeting = _copy_dict(meeting)
        self._health = _copy_dict(payload.get("health", {}))
        self._transcript = self._build_transcript()
        self._speakers = self._build_speakers()
        minutes_payload = self.events["minutes"].get("payload")
        self._minutes = (
            _copy_dict(minutes_payload["minutes"])
            if isinstance(minutes_payload, dict)
            and isinstance(minutes_payload.get("minutes"), dict)
            else None
        )
        self.deleted = False

    def _build_transcript(self) -> dict[str, Any]:
        event = self.events["reconciled"]
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("transcript-reconciled fixture payload 无效")
        return {
            "meeting_id": self.meeting_id,
            "transcript_revision": int(payload.get("transcript_revision", 0)),
            "content_revision": int(payload.get("content_revision", 0)),
            "segments": _copy_dict({"segments": payload.get("segments", [])})[
                "segments"
            ],
        }

    def _build_speakers(self) -> dict[str, dict[str, Any]]:
        speakers: dict[str, dict[str, Any]] = {}
        for segment in self._transcript["segments"]:
            if not isinstance(segment, dict):
                continue
            key = str(segment.get("speaker_key") or "")
            if not key or key in speakers:
                continue
            label = str(segment.get("speaker_name") or key)
            speakers[key] = {
                "speaker_key": key,
                "default_label": label,
                "display_name": label,
                "updated_at": self.events["snapshot"]["occurred_at"],
            }
        speaker_event = self.events["speaker"].get("payload")
        if isinstance(speaker_event, dict):
            key = str(speaker_event.get("speaker_key") or "")
            if key in speakers and isinstance(speaker_event.get("display_name"), str):
                speakers[key]["display_name"] = speaker_event["display_name"]
                speakers[key]["updated_at"] = self.events["speaker"]["occurred_at"]
        return speakers

    def _ensure_available(self) -> None:
        if self.deleted:
            raise KeyError(self.meeting_id)

    def runtime(self) -> dict[str, Any]:
        self._ensure_available()
        return {
            "pipeline": "running",
            "subtitle": "connected",
            "mic_muted": bool(self._health.get("mic_muted", False)),
            "persona": None,
            "voice": "default",
            "duplex_mode": "speaker_focus",
            "session_started_at": self._meeting.get("started_at"),
            "mode": "meeting",
            "pcm_owner": "meeting",
            "active_meeting_id": self.meeting_id,
            "meeting_state": self._meeting.get("status", "recording"),
            "meeting_started_at": self._meeting.get("started_at"),
            "storage": self._health.get("storage", "ok"),
            "runtime_revision": 1,
        }

    def summary(self) -> dict[str, Any]:
        self._ensure_available()
        keys = (
            "id",
            "title",
            "status",
            "language",
            "started_at",
            "ended_at",
            "transcript_revision",
            "content_revision",
            "interruption_reason",
            "created_at",
        )
        summary = {key: self._meeting.get(key) for key in keys}
        summary["id"] = self.meeting_id
        summary["transcript_revision"] = self._transcript["transcript_revision"]
        summary["content_revision"] = self._transcript["content_revision"]
        return summary

    def detail(self) -> dict[str, Any]:
        result = self.summary()
        result.update(
            {
                "audio_source": "microphone",
                "metadata": {},
                "speakers": _copy_dict(self._speakers),
                "latest_minutes": _copy_dict(self._minutes) if self._minutes else None,
                "updated_at": self.events["snapshot"]["occurred_at"],
            }
        )
        return result

    def transcript(self) -> dict[str, Any]:
        self._ensure_available()
        return _copy_dict(self._transcript)

    def minutes(self, version: int) -> dict[str, Any] | None:
        self._ensure_available()
        if self._minutes is None or int(self._minutes.get("version", 0)) != version:
            return None
        return _copy_dict(self._minutes)

    def update_title(self, title: str) -> dict[str, Any]:
        self._ensure_available()
        normalized = title.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("title 无效")
        self._meeting["title"] = normalized
        return self.detail()

    def rename_speaker(self, speaker_key: str, display_name: str) -> dict[str, Any] | None:
        self._ensure_available()
        speaker = self._speakers.get(speaker_key)
        normalized = display_name.strip()
        if speaker is None or not normalized or len(normalized) > 200:
            return None
        speaker["display_name"] = normalized
        self._transcript["content_revision"] += 1
        self._meeting["content_revision"] = self._transcript["content_revision"]
        speaker["updated_at"] = self.events["speaker"]["occurred_at"]
        return _copy_dict(speaker)

    def delete(self) -> None:
        self.deleted = True

    def sequence(self, scenario: str) -> list[dict[str, Any]]:
        names_by_scenario = {
            "happy_path": (
                "snapshot",
                "partial",
                "reconciled",
                "health",
                "gap",
                "speaker",
                "resync",
                "finalizing",
                "completed",
                "minutes",
            ),
            "partial": ("snapshot", "partial"),
            "revision_gap": ("snapshot", "revision_gap"),
            "error": ("snapshot", "health", "gap"),
            "disconnect": ("snapshot", "partial", "reconciled"),
        }
        if scenario == "meeting_switch":
            return [self._second_snapshot()]
        names = names_by_scenario.get(scenario)
        if names is None:
            raise ValueError(f"未知 mock scenario: {scenario}")
        return [_copy_dict(self.events[name]) for name in names]

    def _second_snapshot(self) -> dict[str, Any]:
        event = _copy_dict(self.events["snapshot"])
        event["event_id"] = "00000000-0000-4000-8000-000000000016"
        event["meeting_id"] = _SECOND_MEETING_ID
        event["payload"]["meeting"]["id"] = _SECOND_MEETING_ID
        event["payload"]["meeting"]["title"] = "第二场联调"
        event["payload"]["meeting"]["transcript_revision"] = 0
        event["payload"]["meeting"]["content_revision"] = 0
        event["payload"]["transcript_revision"] = 0
        event["payload"]["content_revision"] = 0
        event["payload"]["partial"] = None
        return event


def create_contract_mock_app(fixture_dir: Path | None = None) -> FastAPI:
    """创建无外部状态的契约 mock ASGI 应用。"""

    store = MeetingFixtureStore(fixture_dir)
    app = FastAPI(title="Voice Studio Meeting Contract Mock", version="1.0.0")
    app.state.meeting_fixture_store = store
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|"
            r"10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+)(:\d+)?$"
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "contract-mock", "contract_version": CONTRACT_VERSION}

    @app.get("/api/runtime")
    @app.get("/api/v1/runtime")
    async def runtime() -> dict[str, Any]:
        return store.runtime()

    @app.get("/api/services")
    async def services() -> dict[str, Any]:
        return {
            "network_scope": "local",
            "services": [
                {"name": "speechrail", "status": "ok", "url": "http://127.0.0.1:8201"},
                {"name": "tts", "status": "ok", "url": "http://127.0.0.1:8765"},
                {"name": "lm", "status": "ok", "url": "http://127.0.0.1:1234"},
            ],
            "diagnostics": {"mode": "contract-mock"},
        }

    @app.get("/api/v1/meetings")
    async def list_meetings() -> dict[str, Any]:
        try:
            return {"items": [store.summary()], "next_cursor": None}
        except KeyError:
            return {"items": [], "next_cursor": None}

    @app.get("/api/v1/meetings/{meeting_id}")
    async def get_meeting(request: Request, meeting_id: UUID) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        return store.detail()

    @app.patch("/api/v1/meetings/{meeting_id}")
    async def update_meeting_title(
        request: Request, meeting_id: UUID, body: dict[str, Any]
    ) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        try:
            return store.update_title(str(body.get("title", "")))
        except ValueError:
            return _error_response(request, "invalid_request", "标题无效", 400)

    @app.delete("/api/v1/meetings/{meeting_id}")
    async def delete_meeting(request: Request, meeting_id: UUID) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        store.delete()
        return Response(status_code=204)

    @app.post("/api/v1/meetings/{meeting_id}/generate-title")
    async def generate_title(request: Request, meeting_id: UUID) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        return store.update_title("实时转录联调")

    @app.get("/api/v1/meetings/{meeting_id}/transcript")
    async def get_transcript(request: Request, meeting_id: UUID) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        return store.transcript()

    @app.patch("/api/v1/meetings/{meeting_id}/speakers/{speaker_key:path}")
    async def update_speaker(
        request: Request, meeting_id: UUID, speaker_key: str, body: dict[str, Any]
    ) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        speaker = store.rename_speaker(speaker_key, str(body.get("display_name", "")))
        if speaker is None:
            return _error_response(request, "not_found", "说话人不存在", 404)
        return speaker

    @app.post("/api/v1/meetings/{meeting_id}/minutes")
    async def create_minutes(request: Request, meeting_id: UUID) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        minutes = store.minutes(1)
        if minutes is None:
            return _error_response(request, "service_unavailable", "fixture 没有纪要", 503)
        return minutes

    @app.get("/api/v1/meetings/{meeting_id}/minutes/{version}")
    async def get_minutes(
        request: Request, meeting_id: UUID, version: int
    ) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        minutes = store.minutes(version)
        if minutes is None:
            return _error_response(request, "not_found", "纪要版本不存在", 404)
        return minutes

    @app.get("/api/v1/meetings/{meeting_id}/export")
    async def export_meeting(
        request: Request,
        meeting_id: UUID,
        export_format: str = Query(default="md", alias="format"),
    ) -> Any:
        if str(meeting_id) != store.meeting_id or store.deleted:
            return _error_response(request, "not_found", "会议不存在", 404)
        transcript = store.transcript()
        if export_format == "json":
            return JSONResponse(content=transcript)
        lines = [str(segment.get("text", "")) for segment in transcript["segments"]]
        if export_format == "srt":
            content = "\n\n".join(
                f"{index}\n00:00:01,000 --> 00:00:02,000\n{text}"
                for index, text in enumerate(lines, start=1)
            )
            return PlainTextResponse(content, media_type="application/x-subrip")
        content = "# " + str(store.summary()["title"]) + "\n\n" + "\n".join(lines)
        media_type = "text/plain" if export_format == "txt" else "text/markdown"
        return PlainTextResponse(content, media_type=media_type)

    @app.websocket("/ws/v1/meetings")
    async def meeting_events(websocket: WebSocket) -> None:
        await websocket.accept()
        scenario = websocket.query_params.get("scenario", "happy_path")
        try:
            delay_ms = max(0, min(10_000, int(websocket.query_params.get("delay_ms", "0"))))
        except ValueError:
            delay_ms = 0
        try:
            duplicate = int(websocket.query_params["duplicate"])
        except (KeyError, ValueError):
            duplicate = -1
        try:
            disconnect_after = int(websocket.query_params["disconnect_after"])
        except (KeyError, ValueError):
            disconnect_after = -1
        try:
            events = store.sequence(scenario)
        except ValueError as exc:
            await websocket.close(code=1008, reason=str(exc)[:120])
            return
        sent = 0
        try:
            for index, event in enumerate(events):
                await websocket.send_json(event)
                sent += 1
                if index == duplicate:
                    await websocket.send_json(_copy_dict(event))
                    sent += 1
                if disconnect_after >= 0 and sent >= disconnect_after:
                    await websocket.close(code=1012, reason="mock disconnect")
                    return
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/v1/control")
    async def control(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            await websocket.send_json(
                {
                    "contract_version": CONTRACT_VERSION,
                    "event": "runtime_state",
                    "state": store.runtime(),
                }
            )
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {}
                request_id = payload.get("request_id", "") if isinstance(payload, dict) else ""
                command = payload.get("cmd", "") if isinstance(payload, dict) else ""
                await websocket.send_json(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "request_id": request_id,
                        "cmd": command,
                        "ok": True,
                        "state": store.runtime(),
                        "error": None,
                    }
                )
        except WebSocketDisconnect:
            return

    return app


app = create_contract_mock_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动会议助手 v1 契约 mock 服务")
    parser.add_argument("--host", default=os.environ.get("VR_MEETING_MOCK_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("VR_MEETING_MOCK_PORT", "8200")),
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=(
            Path(os.environ["VR_MEETING_MOCK_FIXTURE_DIR"])
            if os.environ.get("VR_MEETING_MOCK_FIXTURE_DIR")
            else None
        ),
    )
    args = parser.parse_args()
    mock_app = create_contract_mock_app(args.fixture_dir)
    uvicorn.run(mock_app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
