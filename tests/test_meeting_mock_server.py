"""会议助手 v1 fixture/mock 联调服务测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sona.meeting.mock_server import create_contract_mock_app


def _client() -> TestClient:
    root = Path(__file__).resolve().parents[1]
    return TestClient(
        create_contract_mock_app(root / "contracts/meeting-assistant/v1/fixtures"),
        raise_server_exceptions=False,
    )


def test_mock_exposes_independent_rest_baseline() -> None:
    with _client() as client:
        runtime = client.get("/api/v1/runtime")
        runtime_alias = client.get("/api/runtime")
        detail = client.get(
            "/api/v1/meetings/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        transcript = client.get(
            "/api/v1/meetings/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/transcript"
        )

    assert runtime.status_code == 200
    assert runtime.json()["active_meeting_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert runtime_alias.status_code == 200
    assert runtime_alias.json() == runtime.json()
    assert detail.status_code == 200
    assert detail.json()["speakers"]["e1:s1"]["display_name"] == "张三"
    assert transcript.status_code == 200
    assert transcript.json()["segments"][0]["speaker_name"] == "说话人 1"


def test_mock_exposes_status_bar_service_contract() -> None:
    with _client() as client:
        response = client.get("/api/services")

    assert response.status_code == 200
    body = response.json()
    assert body["network_scope"] == "local"
    assert {service["name"] for service in body["services"]} == {"speechrail", "tts", "lm"}
    assert all(service["status"] == "ok" for service in body["services"])


def test_mock_allows_loopback_frontend_cors_preflight() -> None:
    with _client() as client:
        response = client.options(
            "/api/v1/meetings",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_mock_replays_revision_gap_scenario() -> None:
    with _client() as client, client.websocket_connect(
        "/ws/v1/meetings?scenario=revision_gap"
    ) as websocket:
        snapshot = websocket.receive_json()
        resync = websocket.receive_json()

    assert snapshot["type"] == "meeting_snapshot"
    assert resync["type"] == "resync_required"
    assert resync["payload"] == {"expected_revision": 4, "reason": "revision_gap"}


def test_mock_supports_deterministic_duplicate_and_http_errors() -> None:
    with _client() as client, client.websocket_connect(
        "/ws/v1/meetings?scenario=partial&duplicate=1"
    ) as websocket:
        events = [websocket.receive_json(), websocket.receive_json(), websocket.receive_json()]
        missing = client.get("/api/v1/meetings/cccccccc-cccc-4ccc-8ccc-cccccccccccc")

    assert [event["type"] for event in events] == [
        "meeting_snapshot",
        "transcript_partial",
        "transcript_partial",
    ]
    assert events[1] == events[2]
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
