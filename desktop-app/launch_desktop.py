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
import sys
import time
import threading
from typing import List, Optional

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
            target_fps=TARGET_FPS,
        )

        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._running = False

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
                self._load_seats()
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

        # Post to dashboard (fire and forget in background)
        threading.Thread(
            target=self.api_client.post_event,
            args=(payload,),
            daemon=True,
        ).start()

    def _on_vision_frame(self, jpeg_bytes: bytes):
        """Store latest JPEG frame for OpenCV window display."""
        with self._frame_lock:
            self._latest_frame = jpeg_bytes

    def _load_sections(self):
        """Fetch sections from dashboard."""
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

    def _load_seats(self):
        """Fetch seat zones from dashboard and push to vision worker."""
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

    def _cycle_section(self):
        """Cycle to next section."""
        if not self.sections:
            return
        self.current_section_idx = (self.current_section_idx + 1) % len(self.sections)
        self.section_id = self.sections[self.current_section_idx]["id"]
        print(f"[CameraNode] Switched to section: {self.sections[self.current_section_idx]['name']}")
        self._load_seats()

    def run(self):
        """Main loop — OpenCV window with live camera feed and HUD overlay."""
        print(f"[CameraNode] Connecting to dashboard: {self.dashboard_url}")

        # Try initial connection
        connected = self.api_client.check_connection()
        if connected:
            print("[CameraNode] Dashboard connection OK")
            self._load_sections()
            self._load_seats()
            # Check for active session
            active = self.api_client.fetch_active_session()
            if active:
                self.vision_worker.set_session_id(active["id"])
                print(f"[CameraNode] Active session found: {active['id']}")
        else:
            print(f"[CameraNode] Dashboard not reachable at {self.dashboard_url} — running offline")

        # Start edge WebSocket
        self.api_client.start_edge_websocket()

        # Start vision worker
        self.vision_worker.start()
        self._running = True

        window_name = "ClassTrack Camera Node"
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 540)

        print("[CameraNode] Press 'Q' to quit | 'C' to cycle sections | 'S' to sync seats | 'R' to reload")

        last_sync_time = time.time()

        while self._running:
            # Periodic background sync / reconnect check
            now = time.time()
            if now - last_sync_time > 3.0:
                last_sync_time = now
                if not self.api_client.is_connected:
                    if self.api_client.check_connection():
                        print("[CameraNode] Dashboard reconnected! Syncing...")
                        self._load_sections()
                        self._load_seats()
                        active = self.api_client.fetch_active_session()
                        if active:
                            self.vision_worker.set_session_id(active.get("id"))
                elif not self.sections:
                    self._load_sections()
                    self._load_seats()

            frame = None
            with self._frame_lock:
                if self._latest_frame is not None:
                    # Decode JPEG to numpy array
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
                self._load_seats()
                print("[CameraNode] Seats reloaded from dashboard")
            elif key == ord("r") or key == ord("R"):
                self._load_sections()
                self._load_seats()
                print("[CameraNode] Full refresh from dashboard")

            # Check if window was closed
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break

        # Cleanup
        self._running = False
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
