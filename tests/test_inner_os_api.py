from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sona.meeting.api import MeetingAPIError, _meeting_error_handler
from sona.meeting.inner_os.api import install_inner_os_api

MEETING_ID = UUID("00000000-0000-0000-0000-000000000001")
EXCHANGE_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeService:
    def __init__(self) -> None:
        self.exchange = {
            "id": EXCHANGE_ID,
            "meeting_id": MEETING_ID,
            "question": "下一步是什么？",
            "intent": "fact",
            "answer": {"intent": "fact"},
            "source_transcript_revision": 1,
            "source_content_revision": 1,
            "used_ephemeral_context": False,
            "model": "local/kat-coder-2.5",
            "reasoning": "off",
            "prompt_version": "inner-os-v1",
        }

    def take_completed(self, exchange_id: UUID):
        if exchange_id == self.exchange["id"]:
            return self.exchange
        return None

    def peek_completed(self, exchange_id: UUID):
        return self.exchange if exchange_id == self.exchange["id"] else None


class FakeRepository:
    def __init__(self) -> None:
        self.saved = []

    async def save(self, exchange):
        self.saved.append(exchange)
        return exchange

    async def get(self, meeting_id, exchange_id):
        for item in self.saved:
            if item["meeting_id"] == meeting_id and item["id"] == exchange_id:
                return item
        return None

    async def list(self, meeting_id, cursor, limit):
        return [], None

    async def delete(self, meeting_id, exchange_id):
        return None


def _client() -> tuple[TestClient, FakeRepository]:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        meeting=SimpleNamespace(inner_os_enabled=True)
    )
    app.state.inner_os_service = FakeService()
    repository = FakeRepository()
    app.state.meeting_repository = object()
    app.state.inner_os_exchange_repository = repository
    app.add_exception_handler(MeetingAPIError, _meeting_error_handler)
    install_inner_os_api(app)
    return TestClient(app, raise_server_exceptions=False), repository


def test_save_list_detail_delete_follow_frontend_contract() -> None:
    client, repository = _client()
    base = f"/api/v1/meetings/{MEETING_ID}/inner-os/exchanges"

    saved = client.put(f"{base}/{EXCHANGE_ID}")
    assert saved.status_code == 201
    assert saved.json()["id"] == str(EXCHANGE_ID)
    assert len(repository.saved) == 1

    repeated = client.put(f"{base}/{EXCHANGE_ID}")
    assert repeated.status_code == 200
    assert len(repository.saved) == 1

    listed = client.get(base)
    assert listed.status_code == 200
    assert listed.json() == {"items": [], "next_cursor": None}

    missing = client.get(f"{base}/{uuid4()}")
    assert missing.status_code == 404

    deleted = client.delete(f"{base}/{EXCHANGE_ID}")
    assert deleted.status_code == 204


def test_save_rejects_cross_meeting_exchange_without_request_body() -> None:
    client, repository = _client()
    other_meeting = UUID("00000000-0000-0000-0000-000000000003")
    response = client.put(
        f"/api/v1/meetings/{other_meeting}/inner-os/exchanges/{EXCHANGE_ID}",
        json={"question": "tamper"},
    )
    assert response.status_code == 404
    assert repository.saved == []
    assert response.json()["error"]["code"] == "inner_os_not_found"

    saved = client.put(f"/api/v1/meetings/{MEETING_ID}/inner-os/exchanges/{EXCHANGE_ID}")
    assert saved.status_code == 201
