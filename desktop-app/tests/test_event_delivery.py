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


def test_camera_ignores_other_class_commands_and_polls_selected_section(monkeypatch):
    messages = []
    client = DashboardAPIClient(section_id='section-a', on_session_command=messages.append)
    client.session_id = 'session-a'
    client._handle_ws_message({'type': 'SESSION_STARTED', 'section_id': 'section-b', 'session_id': 'session-b'})
    client._handle_ws_message({'type': 'SESSION_STOPPED', 'section_id': 'section-b'})
    assert client.session_id == 'session-a'
    assert messages == []
    get = Mock(return_value=Mock(status_code=200, json=lambda: {'id': 'session-a', 'section_id': 'section-a'}))
    monkeypatch.setattr('api_client.requests.get', get)
    assert client.fetch_active_session_result()[0]
    assert get.call_args.kwargs['params'] == {'section_id': 'section-a'}


def test_selecting_camera_section_notifies_server():
    import json
    client = DashboardAPIClient(section_id='section-a')
    client._ws_connected = True
    client._ws = Mock()
    client.select_section('section-b')
    assert client.section_id == 'section-b'
    assert json.loads(client._ws.send.call_args.args[0]) == {'type': 'SELECT_SECTION', 'section_id': 'section-b'}
