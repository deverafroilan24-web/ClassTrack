"""
API Client for Camera Node → Web Dashboard Communication.

Handles:
- Fetching sections and seat configurations from the remote dashboard
- Posting gesture events via REST (POST /api/events/ingest)
- Persistent WebSocket connection to /ws/edge for bidirectional comms
"""

import json
from urllib.parse import urlparse, urlencode
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests


class DashboardAPIClient:
    """
    REST + WebSocket client for communicating with the remote Web Dashboard.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        on_session_command: Optional[Callable[[dict], None]] = None,
        section_id: str = "",
        follow_dashboard: Optional[bool] = None,
    ):
        self.section_id = section_id
        self.base_url = base_url.rstrip("/")
        self.follow_dashboard = (urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"}
                                 and not section_id) if follow_dashboard is None else follow_dashboard
        self._context_supported: Optional[bool] = None
        self._context_generation = None
        self._selection_revision = -1
        self._context_update_counter = 0
        self._command_lock = threading.RLock()
        self.on_session_command = on_session_command
        self._http_connected = False
        self._ws_connected = False
        self._access_denied = False
        self._ws = None
        self._ws_lock = threading.Lock()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_running = False
        self._session_id: Optional[str] = None
        self._event_queue: queue.Queue = queue.Queue()
        self._event_thread: Optional[threading.Thread] = None
        self._event_ingest_version = 1

    @property
    def is_connected(self) -> bool:
        return not self._access_denied and (self._ws_connected or self._http_connected)

    @property
    def access_denied(self) -> bool:
        return self._access_denied

    def _record_access_status(self, status_code: int) -> None:
        if status_code in (401, 403):
            self._access_denied = True
            self._http_connected = False
        elif status_code == 200:
            self._access_denied = False
            self._http_connected = True

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_connected

    @property
    def is_http_connected(self) -> bool:
        return self._http_connected

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @session_id.setter
    def session_id(self, val: Optional[str]):
        self._session_id = val

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # REST Endpoints
    # ------------------------------------------------------------------

    def fetch_sections(self) -> List[Dict[str, Any]]:
        """GET /api/sections"""
        try:
            resp = requests.get(f"{self.base_url}/api/sections",
                                params={"section_id": self.section_id} if self.section_id else {},
                                headers=self._headers(), timeout=5)
            self._record_access_status(resp.status_code)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[APIClient] Failed to fetch sections: {e}")
            return []

    def fetch_seats(self, section_id: str = "") -> Optional[List[Dict[str, Any]]]:
        """GET /api/seats?section_id=..."""
        params = {"section_id": section_id} if section_id else {}
        last_error = None
        # Render may briefly take several seconds to wake or answer REST calls.
        # Retry transient failures so a single timeout does not leave the camera
        # without its section roster for the remainder of the session.
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{self.base_url}/api/seats",
                    params=params,
                    headers=self._headers(),
                    timeout=(5, 15),
                )
                self._record_access_status(resp.status_code)
                if resp.status_code in (401, 403):
                    print("[APIClient] Camera access denied; deploy the dashboard update that allows camera connections without a key.")
                    return None
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        print(f"[APIClient] Failed to fetch seats after retries: {last_error}")
        return None

    def fetch_active_session(self) -> Optional[Dict[str, Any]]:
        """GET /api/sessions/active"""
        _, session = self.fetch_active_session_result()
        return session

    def fetch_camera_context_result(self) -> tuple[bool, Optional[dict]]:
        """Reconcile the server's class/session selection together, never by name/order.

        Older deployed backends retain the legacy path until they are updated.
        A slow response cannot overwrite a newer socket control message.
        """
        if self._context_supported is False:
            return False, None
        counter = self._context_update_counter
        params = {"follow_dashboard": "true" if self.follow_dashboard else "false"}
        if self.section_id:
            params['section_id'] = self.section_id
        try:
            response = requests.get(f"{self.base_url}/api/edge/context", params=params,
                                    headers=self._headers(), timeout=5)
            if response.status_code == 404:
                self._context_supported = False
                return False, None
            self._record_access_status(response.status_code)
            response.raise_for_status()
            context = response.json()
            self._context_supported = True
            self._handle_ws_message(context, expected_counter=counter)
            return True, context
        except requests.RequestException as error:
            print(f"[APIClient] Failed to fetch camera context: {error}")
            return False, None

    def fetch_active_session_result(self) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Distinguish an idle dashboard from a failed request."""
        try:
            resp = requests.get(f"{self.base_url}/api/sessions/active", params={"section_id": self.section_id} if self.section_id else {}, headers=self._headers(), timeout=5)
            self._record_access_status(resp.status_code)
            if resp.status_code == 200:
                data = resp.json()
                self._session_id = data.get("id") if data else None
                return True, data
            print(f"[APIClient] Active session request returned {resp.status_code}")
        except Exception as e:
            print(f"[APIClient] Failed to fetch active session: {e}")
        return False, None

    def fetch_camera_settings(self) -> Optional[dict]:
        """Recover the selected classroom's controls after missed socket messages."""
        try:
            response = requests.get(
                f"{self.base_url}/api/camera/settings",
                params={"section_id": self.section_id} if self.section_id else {},
                headers=self._headers(), timeout=5,
            )
            response.raise_for_status()
            data = response.json()
            self._record_event_capability(data)
            return data.get("settings")
        except requests.RequestException as error:
            print(f"[APIClient] Failed to fetch camera settings: {error}")
            return None

    def _record_event_capability(self, message: dict) -> None:
        version = message.get("event_ingest_version", 1)
        self._event_ingest_version = version if isinstance(version, int) and version >= 1 else 1

    def _event_payload(self, event_payload: dict) -> dict:
        """Send optional metadata only to dashboards advertising its schema."""
        payload = dict(event_payload)
        if self._event_ingest_version < 2:
            payload.pop("verification_status", None)
            payload.pop("face_similarity", None)
        return payload

    def post_event(self, event_payload: dict) -> bool:
        """POST /api/events/ingest — send a gesture event to the dashboard."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/events/ingest",
                json=self._event_payload(event_payload),
                headers=self._headers(),
                timeout=3,
            )
            if resp.status_code == 200:
                return True
            else:
                print(f"[APIClient] Event ingest returned {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            print(f"[APIClient] Failed to post event: {e}")
            return False

    def send_event(self, event_payload: dict) -> bool:
        """Queue events in camera order; REST confirms persistence before the next event."""
        if self._event_thread is None or not self._event_thread.is_alive():
            self._event_thread = threading.Thread(target=self._event_loop, daemon=True, name="EdgeEventSender")
            self._event_thread.start()
        self._event_queue.put_nowait(dict(event_payload))
        return True

    def _event_loop(self):
        while True:
            payload = self._event_queue.get()
            try:
                attempt = 0
                while True:
                    try:
                        response = requests.post(
                            f"{self.base_url}/api/events/ingest",
                            json=self._event_payload(payload), headers=self._headers(), timeout=5,
                        )
                        if response.status_code == 200:
                            break
                        if response.status_code in (400, 401, 403, 404, 409, 422):
                            print(f"[APIClient] Event rejected ({response.status_code}): {response.text}")
                            break
                        if attempt == 0 or attempt % 6 == 0:
                            print(f"[APIClient] Event delivery failed ({response.status_code}); retrying")
                    except requests.RequestException as exc:
                        if attempt == 0 or attempt % 6 == 0:
                            print(f"[APIClient] Event delivery error: {exc}; retrying")
                    attempt += 1
                    time.sleep(min(2 ** min(attempt - 1, 3), 8))
            finally:
                self._event_queue.task_done()

    def sync_seats(self, seats_data: list, section_id: str = "") -> bool:
        """PUT /api/seats — push calibrated seat coordinates to dashboard."""
        try:
            params = {}
            if section_id:
                params["section_id"] = section_id
            resp = requests.put(
                f"{self.base_url}/api/seats",
                json={"seats": seats_data},
                params=params,
                headers=self._headers(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception as e:
            print(f"[APIClient] Failed to sync seats: {e}")
            return False

    def check_connection(self) -> bool:
        """Check camera access to the roster, rather than public server health."""
        try:
            resp = requests.get(f"{self.base_url}/api/sections", headers=self._headers(), timeout=5)
            self._record_access_status(resp.status_code)
            self._http_connected = resp.status_code == 200
            return self._http_connected
        except Exception:
            self._http_connected = False
            return False

    # ------------------------------------------------------------------
    # WebSocket Edge Connection
    # ------------------------------------------------------------------

    def start_edge_websocket(self):
        """Start persistent WebSocket connection to /ws/edge in background thread."""
        if self._ws_running:
            return
        self._ws_running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True, name="EdgeWSThread")
        self._ws_thread.start()

    def stop_edge_websocket(self):
        self._ws_running = False

    def _ws_loop(self):
        """Background WebSocket connection loop with auto-reconnect and backoff."""
        try:
            import websocket
        except ImportError:
            print("[APIClient] websocket-client not installed. Using REST-only mode.")
            return

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/edge"
        backoff_sec = 3.0

        while self._ws_running:
            try:
                ws = websocket.WebSocket()
                params = {}
                if self.section_id:
                    params['section_id'] = self.section_id
                if self.follow_dashboard:
                    params['follow_dashboard'] = '1'
                ws.connect(ws_url + ("?" + urlencode(params) if params else ""), timeout=5)
                with self._ws_lock:
                    self._ws = ws
                self._ws_connected = True
                self._access_denied = False
                backoff_sec = 3.0  # reset backoff on success
                print(f"[APIClient] Connected to dashboard WebSocket: {ws_url}")

                while self._ws_running:
                    try:
                        ws.settimeout(5.0)
                        data = ws.recv()
                        if not data:
                            break
                        if data in ("pong", "ping"):
                            continue
                        try:
                            msg = json.loads(data)
                            self._handle_ws_message(msg)
                        except json.JSONDecodeError:
                            # Not JSON, ignore safely without disconnecting
                            continue
                    except websocket.WebSocketTimeoutException:
                        # Send heartbeat
                        try:
                            ws.send("ping")
                        except Exception:
                            break
                    except Exception as e:
                        print(f"[APIClient] WS receive error: {e}")
                        break

                with self._ws_lock:
                    self._ws = None
                ws.close()
            except Exception as e:
                with self._ws_lock:
                    self._ws = None
                print(f"[APIClient] WS connection failed: {e}")

            self._ws_connected = False
            if self._ws_running:
                time.sleep(backoff_sec)
                backoff_sec = min(12.0, backoff_sec * 1.5)

    def select_section(self, section_id, manual=False):
        binding_changed = manual and self.follow_dashboard
        if manual:
            self.follow_dashboard = False
        if not section_id or (section_id == self.section_id and not binding_changed):
            return
        self.section_id = section_id
        self._session_id = None
        self._context_update_counter += 1
        with self._ws_lock:
            if self._ws_connected and self._ws:
                try:
                    self._ws.send(json.dumps({'type': 'SELECT_SECTION', 'section_id': section_id,
                                              'follow_dashboard': self.follow_dashboard}))
                except Exception as error:
                    self._ws_connected = False
                    print(f"[APIClient] Section selection will retry on reconnect: {error}")

    def _handle_ws_message(self, msg: dict, expected_counter=None):
        """Handle commands from the web dashboard."""
        # Commands and REST snapshots share one ordered application path.
        # select_section never takes this lock: a callback holds the camera's
        # section lock, so taking both in opposite order could deadlock.
        with self._command_lock:
            if expected_counter is not None and expected_counter != self._context_update_counter:
                return
            self._apply_ws_message(msg)

    def _apply_ws_message(self, msg: dict):
        msg_type = msg.get("type", "")

        control = msg_type in {"EDGE_INIT", "SECTION_SELECTED", "SESSION_STARTED", "SESSION_STOPPED"}
        if (msg_type == 'EDGE_INIT' and not self.follow_dashboard and self.section_id
                and msg.get('selected_section_id') and msg['selected_section_id'] != self.section_id):
            return
        if msg.get("section_id") and self.section_id and msg["section_id"] != self.section_id:
            return

        revision = msg.get('selection_revision')
        generation = msg.get('context_generation')
        if control and isinstance(revision, int):
            if generation != self._context_generation:
                self._context_generation = generation
                self._selection_revision = -1
            if revision < self._selection_revision:
                return
            self._selection_revision = revision
        if control:
            self._context_update_counter += 1

        if msg_type == "EDGE_INIT":
            self._record_event_capability(msg)
            if msg.get('camera_binding_version') == 2:
                self._context_supported = True
            active = msg.get("active_session")
            if 'selected_section_id' in msg:
                self.section_id = msg['selected_section_id'] or ''
            if active:
                self._session_id = active.get("id")
                print(f"[APIClient] Active session: {self._session_id}")
            else:
                self._session_id = None
                print("[APIClient] No active session on dashboard.")

        elif msg_type == "SESSION_STARTED":
            self._session_id = msg.get("session_id")
            print(f"[APIClient] Session started: {self._session_id}")

        elif msg_type == "SESSION_STOPPED":
            self._session_id = None
            print("[APIClient] Session stopped.")

        # Forward to callback if registered
        if self.on_session_command:
            self.on_session_command(msg)
