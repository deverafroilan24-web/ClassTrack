"""Camera access failures must never be presented as a working classroom connection."""

import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api_client import DashboardAPIClient


def test_public_health_does_not_make_unauthorized_camera_online(monkeypatch):
    def get(url, **kwargs):
        return Mock(status_code=200 if url.endswith('/api/edge/status') else 401)

    monkeypatch.setattr('api_client.requests.get', get)
    client = DashboardAPIClient()
    assert not client.check_connection()
    assert not client.is_connected
    assert client.access_denied


def test_camera_can_load_session_without_a_shared_key(monkeypatch):
    def get(url, **kwargs):
        assert 'X-Edge-Key' not in kwargs['headers']
        return Mock(status_code=200, json=lambda: {'id': 'session-a', 'section_id': 'section-a'})

    monkeypatch.setattr('api_client.requests.get', get)
    client = DashboardAPIClient()
    assert client.check_connection()
    assert client.is_connected
    assert not client.access_denied
    assert client.fetch_active_session_result() == (True, {'id': 'session-a', 'section_id': 'section-a'})


def test_auth_rejection_keeps_session_distinct_from_an_ended_session(monkeypatch):
    monkeypatch.setattr('api_client.requests.get', lambda *args, **kwargs: Mock(status_code=403))
    client = DashboardAPIClient()
    client.session_id = 'session-a'
    client._http_connected = True
    assert client.fetch_active_session_result() == (False, None)
    assert client.session_id == 'session-a'
    assert not client.is_connected
    assert client.access_denied


def test_shortcut_uses_config_next_to_packaged_executable(monkeypatch, tmp_path):
    load = Mock()
    monkeypatch.setattr('dotenv.load_dotenv', load)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'ClassTrack.exe'))
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'config.py'))
    load.assert_called_once_with(tmp_path / '.env')


def test_closed_socket_disconnects_without_an_empty_receive_loop(monkeypatch):
    import websocket
    socket = Mock()
    socket.recv.return_value = ''
    monkeypatch.setattr(websocket, 'WebSocket', lambda: socket)
    client = DashboardAPIClient()
    client._ws_running = True
    monkeypatch.setattr('api_client.time.sleep', lambda _: setattr(client, '_ws_running', False))
    client._ws_loop()
    assert socket.connect.call_args.kwargs == {'timeout': 5}
    socket.close.assert_called_once()
    assert not client.is_ws_connected
    assert client._ws is None
