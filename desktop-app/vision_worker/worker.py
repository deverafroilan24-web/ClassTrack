import cv2
import numpy as np
import threading
import time
from typing import Callable, Dict, List, Optional

from vision_worker.face_engine import FaceEngine
from vision_worker.kinematics import KinematicEvaluation
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.pose_engine import SeatZone, YOLOPoseEngine
from vision_worker.state_machine import GestureEventPayload, HandRaiseStateMachine


class VisionWorker:
    """
    Continuous background vision worker.
    Runs single-pass pose estimation, biomechanical kinematic checks,
    temporal debouncing, and podium queue ordering.
    Emits events and video frames directly to callbacks.

    Edge mode: construct with on_event=CameraNodeApp._on_vision_event so
    GestureEventPayloads are formatted as EdgeEventIngest and POSTed to
    the remote web-dashboard via DashboardAPIClient.post_event, instead
    of writing to a local database.
    """

    def __init__(
        self,
        model_path: str = "yolo11s-pose.pt",
        video_source: int | str = 0,
        confidence: float = 0.50,
        debounce_sec: float = 0.35,
        on_event: Optional[Callable[[GestureEventPayload], None]] = None,
        on_frame: Optional[Callable[[bytes], None]] = None,
        target_fps: int = 30,
    ):
        self.video_source = video_source
        self.on_event = on_event
        self.on_frame = on_frame
        self.target_fps = target_fps
        self.frame_interval = 1.0 / target_fps if target_fps > 0 else 0.033

        self.model_path = model_path
        self.confidence = confidence
        self.pose_engine = YOLOPoseEngine(model_path=model_path, confidence=confidence)
        self.podium_queue = PodiumQueueManager()
        self.state_machine = HandRaiseStateMachine(
            podium_queue=self.podium_queue,
            on_event_callback=self._handle_event,
            debounce_duration_sec=debounce_sec,
        )

        try:
            self.face_engine: Optional[FaceEngine] = FaceEngine()
        except Exception as e:
            print(f"[VisionWorker] FaceEngine could not be initialized: {e}")
            self.face_engine = None

        self.show_skeleton: bool = False
        self.seats: List[SeatZone] = []
        self.session_id: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def set_show_skeleton(self, show: bool) -> None:
        with self._lock:
            self.show_skeleton = bool(show)

    def set_confidence(self, conf: float) -> None:
        with self._lock:
            self.confidence = float(conf)
            if self.pose_engine:
                self.pose_engine.confidence = float(conf)

    def switch_model(self, model_name: str) -> bool:
        """
        Hot-swaps the underlying YOLO Pose model weights on the GPU.
        Preserves active seat tracking state across the transition.
        """
        allowed = {
            "yolov8n-pose.pt",
            "yolov8s-pose.pt",
            "yolo11s-pose.pt",
            "yolov8m-pose.pt",
        }
        if model_name not in allowed:
            return False

        with self._lock:
            try:
                print(f"[VisionWorker] Loading {model_name} onto GPU...")
                new_engine = YOLOPoseEngine(model_path=model_name, confidence=self.confidence)
                # Retain existing track coordinates so bounding boxes don't blink
                new_engine.seat_tracks = dict(self.pose_engine.seat_tracks)
                self.pose_engine = new_engine
                self.model_path = model_name
                print(f"[VisionWorker] Successfully switched model to {model_name}")
                return True
            except Exception as e:
                print(f"[VisionWorker] Failed to switch model to {model_name}: {e}")
                return False

    def get_model_info(self) -> dict:
        with self._lock:
            return {
                "active_model": self.model_path,
                "available_models": [
                    {"id": "yolo11s-pose.pt", "name": "YOLO11 Small", "desc": "Recommended for Teacher Laptop (46 FPS, High Precision)", "recommended": True},
                    {"id": "yolov8s-pose.pt", "name": "YOLOv8 Small", "desc": "Fast & Balanced (~52 FPS)", "recommended": False},
                    {"id": "yolov8m-pose.pt", "name": "YOLOv8 Medium", "desc": "Maximum Precision (~25 FPS)", "recommended": False},
                    {"id": "yolov8n-pose.pt", "name": "YOLOv8 Nano", "desc": "Ultra-lightweight (~130 FPS)", "recommended": False},
                ],
            }

    def _handle_event(self, event: GestureEventPayload) -> None:
        if self.on_event:
            self.on_event(event)

    def set_seats(self, seats: List[SeatZone]) -> None:
        with self._lock:
            self.seats = list(seats)

    def set_session_id(self, session_id: Optional[str]) -> None:
        with self._lock:
            self.session_id = session_id
            if session_id is None:
                self.state_machine.reset_all()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="VisionWorkerThread")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _open_capture(self):
        if str(self.video_source).lower() in {"mock", "none", "test", "-1"}:
            return None
        try:
            src = int(self.video_source)
            from vision_worker.camera_source import open_camera_capture
            try:
                cap, idx = open_camera_capture(
                    cv2,
                    preferred_index=src,
                    fallback_index=1 if src == 0 else 0,
                    width=640,
                    height=480,
                    low_latency=True,
                    auto_scan=True,
                )
                print(f"[VisionWorker] Camera opened successfully on index {idx}")
                return cap
            except RuntimeError as e:
                return None
        except ValueError:
            # File source (path string or url)
            try:
                from vision_worker.camera_source import open_video_capture
                cap = open_video_capture(cv2, str(self.video_source), loop=True, realtime=True)
                return cap
            except Exception as e:
                print(f"[VisionWorker] Video file source error: {e}")
                return None
        except Exception as e:
            print(f"[VisionWorker] Unexpected error opening capture: {e}")
            return None

    def _run_loop(self) -> None:
        cap = self._open_capture()
        last_reconnect_attempt = time.monotonic()
        consecutive_read_failures = 0

        while self._running:
            loop_start = time.monotonic()
            ret, frame = False, None

            if cap is not None:
                try:
                    ret, frame = cap.read()
                except Exception:
                    ret, frame = False, None

            if ret and frame is not None:
                consecutive_read_failures = 0
            else:
                consecutive_read_failures += 1
                # If reading video file reached end, reset loop
                if cap is not None and isinstance(self.video_source, str) and not str(self.video_source).isdigit():
                    try:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = cap.read()
                    except Exception:
                        ret, frame = False, None

                # If capture was open but failed multiple times, release it to allow clean reconnect
                if cap is not None and consecutive_read_failures > 15:
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None

                # Automatic periodic background reconnection every 3 seconds if camera is missing
                now = time.monotonic()
                if cap is None and (now - last_reconnect_attempt > 3.0):
                    last_reconnect_attempt = now
                    new_cap = self._open_capture()
                    if new_cap is not None:
                        cap = new_cap
                        consecutive_read_failures = 0

                if not ret or frame is None:
                    # High-tech diagnostic standby HUD (640x480)
                    frame = np.zeros((480, 640, 3), dtype=np.uint8)
                    # Subtle grid lines
                    for gy in range(0, 480, 40):
                        cv2.line(frame, (0, gy), (640, gy), (22, 22, 26), 1)
                    for gx in range(0, 640, 40):
                        cv2.line(frame, (gx, 0), (gx, 480), (22, 22, 26), 1)

                    # Top warning banner
                    cv2.rectangle(frame, (40, 60), (600, 110), (35, 30, 20), -1)
                    cv2.rectangle(frame, (40, 60), (600, 110), (0, 180, 255), 2)
                    cv2.putText(
                        frame,
                        "CAMERA STANDBY / NOT DETECTED",
                        (90, 95),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 215, 255),
                        2,
                    )

                    # Checklist box
                    cv2.rectangle(frame, (40, 130), (600, 370), (28, 28, 32), -1)
                    cv2.rectangle(frame, (40, 130), (600, 370), (60, 60, 70), 1)

                    cv2.putText(frame, "HARDWARE & CONNECTION CHECKLIST:", (60, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 200, 50), 2)
                    cv2.putText(frame, "1. MSI Laptop: Press Fn + F6 to power on internal camera", (60, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (230, 230, 230), 1)
                    cv2.putText(frame, "2. External USB Camera: Unplug & reconnect USB cable", (60, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (230, 230, 230), 1)
                    cv2.putText(frame, "3. Physical Shutter: Verify lens slider is fully open", (60, 275), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (230, 230, 230), 1)
                    cv2.putText(frame, "4. Windows Settings: Enable Privacy > Camera access", (60, 310), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (230, 230, 230), 1)
                    cv2.putText(frame, "5. Close any other app using camera (Zoom, Meet, OBS)", (60, 345), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (230, 230, 230), 1)

                    dot_count = int(time.monotonic() * 2) % 4
                    scan_msg = f"Auto-scanning for cameras{'.' * dot_count}"
                    cv2.putText(frame, scan_msg, (220, 420), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 230, 120), 1)

                    if self.on_frame:
                        encode_ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if encode_ok:
                            self.on_frame(buffer.tobytes())

                    elapsed = time.monotonic() - loop_start
                    sleep_time = self.frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue

            h, w = frame.shape[:2]

            with self._lock:
                current_seats = list(self.seats)
                current_session = self.session_id

            # 1. Run GPU Pose Inference (single-pass)
            detections = self.pose_engine.infer_frame(frame)

            # 2. Seat Containment Mapping & Dynamic Tracking
            seat_matches, unassigned_dets = self.pose_engine.match_seats(detections, current_seats, w, h)

            # 3. Process Kinematics through State Machine ONLY if session is ACTIVE!
            now_ms = time.time_ns() // 1_000_000
            if current_session is not None:
                for seat_id, (seat, det, eval_res) in seat_matches.items():
                    # Empty Desk Filter: Skip empty / unassigned desks so they never trigger hand-raise events or occupy podium queue
                    is_empty_desk = (
                        not seat.student_id
                        or not seat.student_name
                        or seat.student_name.startswith("Empty")
                        or not seat.student_name.strip()
                    )
                    if not seat.is_present or is_empty_desk:
                        continue

                    # Anti-Proxy Verification: Check if person raising hand matches assigned student face
                    if eval_res and eval_res.status == "VALID" and seat.face_embedding and self.face_engine:
                        try:
                            enrolled_emb = self.face_engine.json_to_embedding(seat.face_embedding)
                            if enrolled_emb is not None and det is not None:
                                bx1, by1, bx2, by2 = det.bbox
                                head_h = max(25, int((by2 - by1) * 0.45))
                                head_y2 = min(h, by1 + head_h)
                                head_crop = frame[max(0, by1):head_y2, max(0, bx1):min(w, bx2)]
                                if head_crop.size > 0:
                                    res = self.face_engine.extract_face_and_embedding(head_crop)
                                    if res is not None:
                                        _, live_emb = res
                                        is_match, sim = self.face_engine.verify_match(live_emb, enrolled_emb)
                                        if not is_match:
                                            # Convert evaluation to SEAT_MISMATCH to reject hand-raise
                                            eval_res = KinematicEvaluation(
                                                status="INVALID",
                                                reason_code="SEAT_MISMATCH",
                                                arm_angle_deg=eval_res.arm_angle_deg,
                                                is_right_arm=eval_res.is_right_arm,
                                                left_angle_deg=eval_res.left_angle_deg,
                                                right_angle_deg=eval_res.right_angle_deg,
                                            )
                                            seat_matches[seat_id] = (seat, det, eval_res)
                        except Exception:
                            pass

                    self.state_machine.process_seat_frame(
                        seat_id=seat.id,
                        student_name=seat.student_name,
                        evaluation=eval_res,
                        session_id=current_session,
                        now_ms=now_ms,
                    )

            # 4. Prepare Podium Info for Overlay
            podium_entries_map = {}
            if current_session is not None:
                for entry in self.podium_queue.get_podium():
                    podium_entries_map[entry.seat_id] = {
                        "queue_pos": entry.queue_position,
                        "delta_ms": entry.delta_ms,
                    }

            # 5. Render Visual Overlay (Dynamic Person-Anchored Bounding Boxes)
            annotated = self.pose_engine.render_overlay(
                frame=frame,
                seats=current_seats,
                matches=seat_matches,
                unassigned_detections=unassigned_dets,
                podium_entries=podium_entries_map,
                show_skeleton=self.show_skeleton,
                is_session_active=(current_session is not None),
            )

            # 6. Encode JPEG and emit to on_frame callback
            if self.on_frame:
                encode_ok, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if encode_ok:
                    self.on_frame(buffer.tobytes())

            # Maintain target FPS
            elapsed = time.monotonic() - loop_start
            sleep_time = self.frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
