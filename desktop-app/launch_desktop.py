"""
Camera Node Desktop Launcher.

Standalone desktop application that:
1. Runs the YOLO pose estimation pipeline locally
2. Displays the live camera feed with OpenCV window
3. Streams detected gesture events to the remote Web Dashboard
4. Shows connection status and allows section selection

Usage:
    python launch_camera_node.py
    python launch_camera_node.py --cam 1
    python launch_camera_node.py --url https://your-app.onrender.com
"""

import argparse
import os
import sys
import time
import threading
from pathlib import Path
from typing import List, Optional

# When packaged with PyInstaller --noconsole, redirect stdout/stderr to log files or null to prevent crashes
if sys.stdout is None:
    try:
        log_dir = Path.home() / ".classtrack"
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(log_dir / "classtrack.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    try:
        log_dir = Path.home() / ".classtrack"
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(log_dir / "classtrack_error.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        sys.stderr = open(os.devnull, "w")

import cv2
import numpy as np

from config import (
    WEB_DASHBOARD_URL,
    SECTION_ID,
    VIDEO_SOURCE,
    YOLO_MODEL,
    DETECTION_CONFIDENCE,
    TARGET_FPS,
    EDGE_API_KEY,
    resolve_model_path,
)
from api_client import DashboardAPIClient
from vision_worker.pose_engine import SeatZone, YOLOPoseEngine
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.state_machine import GestureEventPayload, HandRaiseStateMachine
from vision_worker.worker import VisionWorker


class CameraNodeApp:
    """
    Desktop Camera Node application.
    Runs the vision pipeline locally and streams events to the cloud dashboard.
    """

    def __init__(
        self,
        dashboard_url: str,
        api_key: str = "",
        video_source: str = "0",
        model_path: str = "yolo11s-pose.pt",
        confidence: float = 0.50,
        section_id: str = "",
    ):
        self.dashboard_url = dashboard_url
        self.section_id = section_id
        self.sections: list = []
        self.current_section_idx = 0

        # Resolve model weights (supports shared repo-root .pt files)
        try:
            model_path = resolve_model_path(model_path)
        except Exception:
            pass

        # API Client for dashboard communication
        self.api_client = DashboardAPIClient(
            base_url=dashboard_url,
            api_key=api_key,
            on_session_command=self._on_session_command,
        )

        # Vision Worker
        self.vision_worker = VisionWorker(
            model_path=model_path,
            video_source=video_source,
            confidence=confidence,
            on_event=self._on_vision_event,
            on_frame=self._on_vision_frame,
            on_raw_frame=self._on_vision_raw_frame,
            target_fps=TARGET_FPS,
        )

        self._latest_raw_frame: Optional[np.ndarray] = None
        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._running = False

        # Background sync queue & worker
        self._sync_trigger = threading.Event()
        self._sync_action: Optional[str] = None
        self._sync_lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None

    def _on_session_command(self, msg: dict):
        """Handle session commands from the dashboard WebSocket."""
        msg_type = msg.get("type", "")
        if msg_type == "EDGE_INIT":
            active = msg.get("active_session")
            if active:
                self.vision_worker.set_session_id(active.get("id"))
                print(f"[CameraNode] Active session: {active.get('id')}")
            else:
                self.vision_worker.set_session_id(None)
            incoming = msg.get("sections", [])
            if incoming:
                self.sections = incoming
                if not self.section_id or not any(s["id"] == self.section_id for s in self.sections):
                    self.section_id = self.sections[0]["id"]
                    self.current_section_idx = 0
                else:
                    for i, s in enumerate(self.sections):
                        if s["id"] == self.section_id:
                            self.current_section_idx = i
                            break
                sec_name = self.sections[self.current_section_idx]["name"] if self.sections else "None"
                print(f"[CameraNode] Synced {len(self.sections)} section(s) via WebSocket. Active: {sec_name}")
                self._trigger_sync("RELOAD_SEATS")
            # Apply initial camera settings if present
            c_settings = msg.get("camera_settings")
            if c_settings:
                self._apply_camera_settings(c_settings)
        elif msg_type == "CAMERA_SETTINGS_UPDATED":
            c_settings = msg.get("settings", {})
            self._apply_camera_settings(c_settings)
        elif msg_type == "SESSION_STARTED":
            session_id = msg.get("session_id")
            self.vision_worker.set_session_id(session_id)
            print(f"[CameraNode] Session started remotely: {session_id}")
        elif msg_type == "SESSION_STOPPED":
            self.vision_worker.set_session_id(None)
            print("[CameraNode] Session stopped remotely.")

    def _apply_camera_settings(self, settings: dict):
        """Apply remote settings from Web Dashboard."""
        if "show_skeleton" in settings:
            show = bool(settings["show_skeleton"])
            self.vision_worker.set_show_skeleton(show)
            print(f"[CameraNode] Remote Setting: Bone structure / Skeleton = {show}")
        if "confidence" in settings:
            conf = float(settings["confidence"])
            self.vision_worker.set_confidence(conf)
            print(f"[CameraNode] Remote Setting: Confidence threshold = {conf:.2f}")
        if "model_name" in settings:
            target_model = settings["model_name"]
            if target_model and target_model != self.vision_worker.model_path:
                try:
                    resolved = resolve_model_path(target_model)
                    swapped = self.vision_worker.switch_model(resolved)
                    if swapped:
                        print(f"[CameraNode] Remote Setting: Hot-swapped AI model to {target_model}")
                except Exception as e:
                    print(f"[CameraNode] Could not switch to model {target_model}: {e}")

    def _on_vision_event(self, event: GestureEventPayload):
        """Called when the vision worker detects a gesture event."""
        session_id = self.api_client.session_id
        if not session_id:
            return

        # Format event payload for the dashboard
        payload = {
            "section_id": self.section_id,
            "seat_id": event.seat_id,
            "student_name": event.student_name,
            "status": event.status,
            "reason_code": event.reason_code,
            "arm_angle_deg": event.arm_angle_deg,
            "duration_sec": event.duration_sec,
            "timestamp_ms": event.timestamp_ms,
            "queue_pos": event.queue_pos,
            "delta_ms": event.delta_ms,
            "session_id": session_id,
            "earned_point": event.earned_point,
        }

        # Send to dashboard in real-time (prefers WebSocket, falls back to REST)
        threading.Thread(
            target=self.api_client.send_event,
            args=(payload,),
            daemon=True,
        ).start()

    def _on_vision_raw_frame(self, frame: np.ndarray):
        """Store latest numpy frame directly for OpenCV window display (zero-copy)."""
        with self._frame_lock:
            self._latest_raw_frame = frame

    def _on_vision_frame(self, jpeg_bytes: bytes):
        """Fallback JPEG frame storage."""
        with self._frame_lock:
            self._latest_frame = jpeg_bytes

    def _trigger_sync(self, action: str):
        """Trigger background network action without stalling UI."""
        with self._sync_lock:
            self._sync_action = action
            self._sync_trigger.set()

    def _load_sections(self):
        """Fetch sections from dashboard (runs in background thread)."""
        try:
            fetched = self.api_client.fetch_sections()
            if fetched:
                self.sections = fetched
                matched = False
                for i, sec in enumerate(self.sections):
                    if sec["id"] == self.section_id:
                        self.current_section_idx = i
                        matched = True
                        break
                if not matched and self.sections:
                    self.section_id = self.sections[0]["id"]
                    self.current_section_idx = 0
                sec_name = self.sections[self.current_section_idx]["name"] if self.sections else "None"
                print(f"[CameraNode] Loaded {len(self.sections)} section(s). Active: {sec_name}")
        except Exception as e:
            print(f"[CameraNode] Could not load sections: {e}")

    def _load_seats(self):
        """Fetch seat zones from dashboard and push to vision worker (runs in background thread)."""
        try:
            seats_data = self.api_client.fetch_seats(self.section_id)
            zones = [
                SeatZone(
                    id=s["id"],
                    label=s["label"],
                    student_name=s["student_name"],
                    student_id_number=s.get("student_id_number", ""),
                    x_min=s["x_min"],
                    y_min=s["y_min"],
                    x_max=s["x_max"],
                    y_max=s["y_max"],
                    is_present=s["is_present"],
                    student_id=s.get("student_id"),
                    photo_path=s.get("photo_path"),
                    face_embedding=s.get("face_embedding"),
                    grid_row=s.get("grid_row", 0),
                    grid_col=s.get("grid_col", 0),
                )
                for s in seats_data
            ]
            self.vision_worker.set_seats(zones)
            print(f"[CameraNode] Loaded {len(zones)} seat zone(s) for section {self.section_id}")
        except Exception as e:
            print(f"[CameraNode] Could not load seats: {e}")

    def _cycle_section(self):
        """Cycle to next section (instant in UI, updates seats in background)."""
        if not self.sections:
            return
        self.current_section_idx = (self.current_section_idx + 1) % len(self.sections)
        self.section_id = self.sections[self.current_section_idx]["id"]
        sec_name = self.sections[self.current_section_idx].get("name", "Unknown")
        print(f"[CameraNode] Switched to section: {sec_name}")
        self._trigger_sync("CYCLE")

    def _sync_worker_loop(self):
        """
        Dedicated background worker for all network communication with dashboard.
        Keeps the OpenCV rendering and UI loop at locked 30–60 FPS without ever hitching.
        """
        # Initial check & load
        try:
            if self.api_client.check_connection():
                print("[CameraNode] Dashboard connection OK")
                self._load_sections()
                self._load_seats()
                active = self.api_client.fetch_active_session()
                if active:
                    self.vision_worker.set_session_id(active.get("id"))
                    print(f"[CameraNode] Active session found: {active.get('id')}")
            else:
                print(f"[CameraNode] Dashboard not reachable at {self.dashboard_url} — running in offline mode")
        except Exception as e:
            print(f"[CameraNode] Initial connect check failed: {e}")

        last_check_time = time.monotonic()
        poll_interval = 8.0

        while self._running:
            # Wait for event signal or periodic poll timeout
            triggered = self._sync_trigger.wait(timeout=1.0)
            if not self._running:
                break

            action = None
            if triggered:
                with self._sync_lock:
                    action = self._sync_action
                    self._sync_action = None
                    self._sync_trigger.clear()

            now = time.monotonic()

            try:
                if action == "RELOAD_ALL":
                    print("[CameraNode] Refreshing dashboard data...")
                    self._load_sections()
                    self._load_seats()
                    active = self.api_client.fetch_active_session()
                    if active:
                        self.vision_worker.set_session_id(active.get("id"))
                elif action in ("RELOAD_SEATS", "CYCLE"):
                    self._load_seats()
                elif now - last_check_time >= poll_interval:
                    last_check_time = now
                    if not self.api_client.is_connected:
                        if self.api_client.check_connection():
                            print("[CameraNode] Dashboard connected! Syncing...")
                            self._load_sections()
                            self._load_seats()
                            active = self.api_client.fetch_active_session()
                            if active:
                                self.vision_worker.set_session_id(active.get("id"))
                            poll_interval = 8.0
                        else:
                            poll_interval = min(20.0, poll_interval * 1.5)
                    elif not self.sections:
                        self._load_sections()
                        if self.sections:
                            self._load_seats()
            except Exception as e:
                print(f"[CameraNode] Background sync error: {e}")
                time.sleep(1.0)

    def run(self):
        """Main loop — OpenCV window with live camera feed and HUD overlay."""
        print(f"[CameraNode] Connecting to dashboard: {self.dashboard_url}")

        self._running = True

        # Start edge WebSocket (background thread)
        self.api_client.start_edge_websocket()

        # Start background sync thread (all network I/O isolated here)
        self._sync_thread = threading.Thread(target=self._sync_worker_loop, daemon=True, name="SyncWorker")
        self._sync_thread.start()

        # Start vision worker
        self.vision_worker.start()

        window_name = "ClassTrack Camera Node"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 540)

        print("[CameraNode] Press 'Q' to quit | 'C' to cycle sections | 'S' to sync seats | 'R' to reload")

        while self._running:
            frame = None
            with self._frame_lock:
                if self._latest_raw_frame is not None:
                    frame = self._latest_raw_frame
                elif self._latest_frame is not None:
                    # Fallback decode JPEG
                    nparr = np.frombuffer(self._latest_frame, np.uint8)
                    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if frame is not None:
                # Add HUD overlay
                self._draw_hud(frame)
                cv2.imshow(window_name, frame)

            # Handle keyboard input
            key = cv2.waitKey(16) & 0xFF  # ~60fps display loop
            if key == ord("q") or key == ord("Q"):
                break
            elif key == ord("c") or key == ord("C"):
                self._cycle_section()
            elif key == ord("s") or key == ord("S"):
                print("[CameraNode] Reloading seats in background...")
                self._trigger_sync("RELOAD_SEATS")
            elif key == ord("r") or key == ord("R"):
                print("[CameraNode] Full refresh requested in background...")
                self._trigger_sync("RELOAD_ALL")

            # Check if window was closed
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break

        # Cleanup
        self._running = False
        self._sync_trigger.set()
        self.vision_worker.stop()
        self.api_client.stop_edge_websocket()
        cv2.destroyAllWindows()
        print("[CameraNode] Shutdown complete.")

    def _draw_hud(self, frame: np.ndarray):
        """Draw minimal status indicator in top corner — no intrusive debug banners."""
        connected = self.api_client.is_connected
        dot_color = (50, 200, 80) if connected else (50, 50, 220)
        cv2.circle(frame, (16, 16), 6, dot_color, -1)
        status_txt = "Online" if connected else "Offline"
        cv2.putText(frame, status_txt, (28, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (240, 240, 240), 1, cv2.LINE_AA)


def main():
    parser = argparse.ArgumentParser(description="ClassTrack Camera Node — Desktop Vision App")
    parser.add_argument("--url", type=str, default=WEB_DASHBOARD_URL, help="Web Dashboard URL")
    parser.add_argument("--cam", type=str, default=VIDEO_SOURCE, help="Camera index or video file path")
    parser.add_argument("--model", type=str, default=YOLO_MODEL, help="YOLO model name")
    parser.add_argument("--section", type=str, default=SECTION_ID, help="Section ID to load")
    parser.add_argument("--confidence", type=float, default=DETECTION_CONFIDENCE, help="Detection confidence")
    args = parser.parse_args()

    print("============================================================")
    print("  ClassTrack Camera Node (Desktop Vision App)               ")
    print(f"  Dashboard URL: {args.url}                                ")
    print(f"  Camera Source: {args.cam}                                ")
    print(f"  YOLO Model:    {args.model}                              ")
    print(f"  Confidence:    {args.confidence}                         ")
    print("============================================================")

    resolved_model = resolve_model_path(args.model)
    if resolved_model != args.model:
        print(f"[CameraNode] Resolved model weights: {resolved_model}")

    app = CameraNodeApp(
        dashboard_url=args.url,
        api_key=EDGE_API_KEY,
        video_source=args.cam,
        model_path=resolved_model,
        confidence=args.confidence,
        section_id=args.section,
    )
    app.run()


if __name__ == "__main__":
    main()
