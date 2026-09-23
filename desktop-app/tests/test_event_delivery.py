"""Camera event delivery must preserve order across transient network failures."""

import sys
import time
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_client import DashboardAPIClient  # noqa: E402


def test_sender_retries_in_order(monkeypatch):
    seen = []
    attempts = {"first": 0}

    def post(url, json, headers, timeout):
        seen.append(json["event_id"])
        if json["event_id"] == "first" and attempts["first"] == 0:
            attempts["first"] += 1
            return Mock(status_code=503, text="temporarily unavailable")
        return Mock(status_code=200, text="ok")

    monkeypatch.setattr("api_client.requests.post", post)
    monkeypatch.setattr("api_client.time.sleep", lambda _: None)
    client = DashboardAPIClient(api_key="key")
    assert client.send_event({"event_id": "first"})
    assert client.send_event({"event_id": "second"})
    deadline = time.monotonic() + 2
    while client._event_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client._event_queue.unfinished_tasks == 0
    assert seen == ["first", "first", "second"]


def test_idle_session_is_distinct_from_network_error(monkeypatch):
    client = DashboardAPIClient()
    monkeypatch.setattr("api_client.requests.get", lambda *args, **kwargs: Mock(status_code=200, json=lambda: None))
    assert client.fetch_active_session_result() == (True, None)
    monkeypatch.setattr("api_client.requests.get", lambda *args, **kwargs: Mock(status_code=503))
    assert client.fetch_active_session_result() == (False, None)
