"""Camera events remain compatible with hosted dashboards during rollout."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_client


def gesture():
    return {"event_id": "raise-1", "state": "RAISED", "reason_code": "VALID",
            "verification_status": "VERIFIED", "face_similarity": 0.91}


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_direct_events_respect_advertised_schema(monkeypatch, version):
    client = api_client.DashboardAPIClient()
    client._record_event_capability({"event_ingest_version": version})
    sent = []
    monkeypatch.setattr(api_client.requests, "post", lambda *args, **kw:
                        sent.append(kw["json"]) or SimpleNamespace(status_code=200))
    original = gesture()
    assert client.post_event(original)
    assert ("verification_status" in sent[0]) == (version >= 2)
    assert sent[0]["event_id"] == original["event_id"]
    assert original["face_similarity"] == 0.91


@pytest.mark.parametrize("metadata", [{}, {"event_ingest_version": 2}])
def test_rest_settings_negotiates_schema(monkeypatch, metadata):
    client = api_client.DashboardAPIClient()
    response = SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"settings": {"show_skeleton": True}, **metadata})
    monkeypatch.setattr(api_client.requests, "get", lambda *args, **kw: response)
    assert client.fetch_camera_settings() == {"show_skeleton": True}
    assert ("face_similarity" in client._event_payload(gesture())) == bool(metadata)


@pytest.mark.parametrize("metadata", [{}, {"event_ingest_version": 2}])
def test_socket_init_refreshes_schema_on_reconnect(metadata):
    client = api_client.DashboardAPIClient()
    client._record_event_capability({"event_ingest_version": 2})
    client._handle_ws_message({"type": "EDGE_INIT", **metadata})
    assert ("face_similarity" in client._event_payload(gesture())) == bool(metadata)


@pytest.mark.parametrize("version", [1, 2])
def test_queued_events_use_negotiated_schema_and_preserve_order(monkeypatch, version):
    client = api_client.DashboardAPIClient()
    client._record_event_capability({"event_ingest_version": version})
    events = [gesture(), {**gesture(), "event_id": "lower-2", "state": "IDLE"}]
    iterator = iter(events)
    completed = []
    client._event_queue = SimpleNamespace(get=lambda: next(iterator), task_done=lambda: completed.append(True))
    sent = []
    monkeypatch.setattr(api_client.requests, "post", lambda *args, **kw:
                        sent.append(kw["json"]) or SimpleNamespace(status_code=200))
    with pytest.raises(StopIteration):
        client._event_loop()
    assert [e["event_id"] for e in sent] == [e["event_id"] for e in events]
    assert all(("face_similarity" in e) == (version >= 2) for e in sent)
    assert len(completed) == 2
