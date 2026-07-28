"""Unit tests for the webhook sink receiver (no DBOS; the waker is injected)."""

from starlette.testclient import TestClient

from adcp_buyer.buyer.sink import build_sink


def test_sink_wakes_with_operation_id_and_payload():
    calls: list[tuple[str, dict]] = []

    def fake_waker(op: str, payload: dict) -> bool:
        calls.append((op, payload))
        return True

    client = TestClient(build_sink(waker=fake_waker))
    resp = client.post("/webhooks/wf-abc123", json={"status": "completed", "media_buy_id": "b1"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "woke": True}
    assert calls == [("wf-abc123", {"status": "completed", "media_buy_id": "b1"})]


def test_sink_tolerates_non_json_body_and_reports_no_wake():
    client = TestClient(build_sink(waker=lambda op, payload: False))
    resp = client.post("/webhooks/wf-x", content="not json")
    assert resp.status_code == 200
    assert resp.json()["woke"] is False
