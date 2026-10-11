import cv2
import numpy as np
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from vision_worker.face_engine import FaceEngine
from vision_worker.face_verifier import FaceVerifier
from vision_worker.gesture_filter import GestureFilter
from vision_worker.kinematics import KinematicEvaluation, evaluate_pose_kinematics
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.question_order import QuestionRaiseOrder
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
        debounce_sec: float = 0.20,
        on_event: Optional[Callable[[GestureEventPayload], None]] = None,
        on_frame: Optional[Callable[[bytes], None]] = None,
        on_raw_frame: Optional[Callable[[np.ndarray], None]] = None,
        target_fps: int = 30,
    ):
        self.video_source = video_source
        self.on_event = on_event
        self.on_frame = on_frame
        self.on_raw_frame = on_raw_frame
        self.target_fps = target_fps
        self.frame_interval = 1.0 / target_fps if target_fps > 0 else 0.033

        self.model_path = model_path
        self.confidence = confidence
        self.pose_engine = YOLOPoseEngine(model_path=model_path, confidence=confidence)
        self.podium_queue = PodiumQueueManager()
        self.question_order = QuestionRaiseOrder()
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
        self.face_verifier = FaceVerifier(self.face_engine) if self.face_engine else None
        self.gesture_filter = GestureFilter()
        self._verification_for_seat = {}
        self._frame_events = None
        self._pending_session_reset = False
        self._pending_tracking_reset = False
        self._clock_origin = None

        self.show_skeleton: bool = False
        self.seats: List[SeatZone] = []
        self._pending_seat_resets: set[str] = set()
        self.session_id: Optional[str] = None
        self._running = False
        self._restart_camera_requested = False
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
        if Path(model_name).name not in allowed:
            return False

        with self._lock:
            confidence = self.confidence
        try:
            print(f"[VisionWorker] Loading {model_name}...")
            new_engine = YOLOPoseEngine(model_path=model_name, confidence=confidence)
            with self._lock:
                new_engine.confidence = self.confidence
                new_engine.seat_tracks = dict(self.pose_engine.seat_tracks)
                if hasattr(self.pose_engine, "tracker"):
                    new_engine.tracker = self.pose_engine.tracker
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
        verification = self._verification_for_seat.get(event.seat_id) if event.status != "IDLE" else None
        event.verification_status = "UNVERIFIED"
        event.face_similarity = 0.0
        if verification is not None and verification.match is not None:
            event.verification_status = "VERIFIED" if verification.match else "SEAT_MISMATCH"
            event.face_similarity = verification.similarity
        if self._frame_events is not None:
            self._frame_events.append(event)
        elif self.on_event:
            self.on_event(event)

    def restart_camera(self) -> None:
        """Trigger immediate camera device reset and re-scan."""
        with self._lock:
            self._restart_camera_requested = True
            self._pending_tracking_reset = True

    def set_seats(self, seats: List[SeatZone]) -> None:
        with self._lock:
            previous = {seat.id: seat for seat in self.seats}
            current = {seat.id: seat for seat in seats}
            for seat_id, old in previous.items():
                new = current.get(seat_id)
                if (new is None or old.student_id != new.student_id
                        or old.student_name != new.student_name
                        or old.is_present != new.is_present
                        or getattr(old, "face_embedding", None) != getattr(new, "face_embedding", None)):
                    self._pending_seat_resets.add(seat_id)
            self.seats = list(seats)

    def set_session_id(self, session_id: Optional[str]) -> None:
        with self._lock:
            if self.session_id != session_id:
                self._pending_session_reset = True
            self.session_id = session_id

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
        if self.face_verifier:
            self.face_verifier.stop()

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

    def _event_time_ms(self, observed_at: float) -> int:
        """Map the monotonic camera clock to epoch time without clock-jump reordering."""
        if self._clock_origin is None:
            self._clock_origin = (observed_at, time.time_ns() // 1_000_000)
        origin_clock, origin_epoch = self._clock_origin
        return origin_epoch + round((observed_at - origin_clock) * 1000)

    def _priority_snapshot(self, matches):
        """Remember original question ordinals, including students who already lowered."""
        for seat_id, (seat, det, evaluation) in matches.items():
            if (det is None or not seat.is_present or not seat.student_id
                    or not evaluation.is_raised_candidate or evaluation.reason_code != "VALID_HAND_RAISE"):
                continue
            started = self.gesture_filter.raise_started_at(det.track_id)
            if started is not None:
                self.question_order.observe(seat, self._event_time_ms(started))
        return self.question_order.snapshot([seat for seat, _, _ in matches.values()])

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        """One detector pass; the same filtered person state feeds UI and events."""
        h, w = frame.shape[:2]

        with self._lock:
            current_seats = list(self.seats)
            current_session = self.session_id
            pending_resets = self._pending_seat_resets
            self._pending_seat_resets = set()
            engine = self.pose_engine
            session_reset = self._pending_session_reset
            tracking_reset = self._pending_tracking_reset
            self._pending_session_reset = False
            self._pending_tracking_reset = False
            show_skeleton = self.show_skeleton
        if session_reset:
            self.state_machine.reset_all()
            self.question_order.clear()
        if session_reset or tracking_reset:
            self.gesture_filter.clear()
            if self.face_verifier:
                self.face_verifier.clear()
        if tracking_reset:
            engine.reset_tracking()
        for seat_id in pending_resets:
            record = engine.seat_tracks.get(seat_id)
            if record:
                self.gesture_filter.reset_person(record["track_id"])
            self.state_machine.reset_seat(seat_id)
            engine.reset_seat(seat_id)

        detections = engine.infer_frame(frame)
        observed_at = time.monotonic()
        seat_matches, unassigned_dets = engine.match_seats(
            detections, current_seats, w, h, frame=frame, now=observed_at)
        now_ms = self._event_time_ms(observed_at)
        for det in unassigned_dets:
            raw_evaluation = evaluate_pose_kinematics(det.pose)
            det.evaluation = (self.gesture_filter.update(det.track_id, raw_evaluation, observed_at)
                              if det.track_id is not None else raw_evaluation)
        self._verification_for_seat.clear()
        self._frame_events = []
        verification_keys = set()
        confirmation_permissions, raise_starts = {}, {}
        for seat_id, (seat, det, evaluation) in list(seat_matches.items()):
            if det is not None:
                evaluation = self.gesture_filter.update(det.track_id, evaluation, observed_at)
            else:
                # Clear an old gesture onset after prolonged absence. Keep the
                # state machine's separate 500 ms queue grace for a missing person.
                record = engine.seat_tracks.get(seat_id)
                if record:
                    self.gesture_filter.update(record["track_id"], evaluation, observed_at)
            verification = None
            allow_confirmation = True
            if det is not None and seat.face_embedding and self.face_verifier:
                verification_keys.add(self.face_verifier.key(seat, det))
                if evaluation.is_raised_candidate:
                    self.face_verifier.request(seat, det, frame, observed_at)
                verification = self.face_verifier.get(seat, det, observed_at)
                allow_confirmation = verification is not None
                if evaluation.is_raised_candidate and verification is not None and verification.match is False:
                    evaluation = KinematicEvaluation(
                        "INVALID", "SEAT_MISMATCH", evaluation.arm_angle_deg,
                        evaluation.active_arm, False, evaluation.left_kinematics, evaluation.right_kinematics)
            self._verification_for_seat[seat_id] = verification
            seat_matches[seat_id] = (seat, det, evaluation)
            confirmation_permissions[seat_id] = allow_confirmation
            raise_starts[seat_id] = self.gesture_filter.raise_started_at(det.track_id) if det else None

        # Resolve all observations before committing confirmations. A pending
        # earlier verification reserves priority; it never delays visual rendering.
        onset_times = {seat_id: self._event_time_ms(started) if started is not None else now_ms
                       for seat_id, started in raise_starts.items()}
        podium_entries_map = self._priority_snapshot(seat_matches) if current_session else {}
        ordered_seats = sorted(seat_matches, key=lambda seat_id:
                               self.state_machine.raise_order_key(seat_id, onset_times[seat_id]))
        for seat_id in ordered_seats:
            seat, det, evaluation = seat_matches[seat_id]
            if current_session and seat.is_present and seat.student_id:
                started = raise_starts[seat_id]
                allow_confirmation = (confirmation_permissions[seat_id] and
                                      not self.state_machine.has_earlier_candidate(seat_id, onset_times[seat_id]))
                self.state_machine.process_seat_frame(
                    seat_id=seat.id, student_name=seat.student_name,
                    evaluation=evaluation, session_id=current_session, now_ms=now_ms,
                    allow_confirmation=allow_confirmation, clock_ms=int(observed_at * 1000),
                    raise_started_ms=self._event_time_ms(started) if started is not None else None,
                    raise_started_clock_ms=int(started * 1000) if started is not None else None,
                    priority_rank=podium_entries_map.get(seat_id, {}).get("queue_pos"),
                    priority_delta_ms=podium_entries_map.get(seat_id, {}).get("delta_ms"))
        # Commit one frame's events in observation order. Same-frame ties follow
        # the same stable identity rule as both queue managers, including when
        # communicating with an older dashboard during rollout.
        frame_events, self._frame_events = self._frame_events, None
        if self.on_event:
            for event in sorted(frame_events, key=lambda event: (event.timestamp_ms, event.seat_id)):
                self.on_event(event)
        self.gesture_filter.prune(observed_at)
        if self.face_verifier:
            # Retain a result during brief occlusion only while its track is live.
            for track in engine.tracker.tracks.values():
                seat = next((seat for seat in current_seats if seat.id == track.seat_id), None)
                if seat and seat.face_embedding:
                    verification_keys.add((seat.id, track.id, seat.face_embedding))
            self.face_verifier.prune(verification_keys)

        # A missing camera/person never means everyone has lowered. Only a
        # sustained, observed all-down posture starts a fresh question automatically.
        if current_session and self.question_order.ready_to_reset(seat_matches, unassigned_dets, observed_at):
            self.question_order.clear()
            self.state_machine.reset_all()
            self.gesture_filter.clear()
            podium_entries_map = {}

        # 5. Render Visual Overlay (Dynamic Person-Anchored Bounding Boxes)
        annotated = engine.render_overlay(
            frame=frame,
            seats=current_seats,
            matches=seat_matches,
            unassigned_detections=unassigned_dets,
            podium_entries=podium_entries_map,
            show_skeleton=show_skeleton,
            is_session_active=(current_session is not None),
        )

        return annotated

    def _process_missing_frame(self) -> None:
        """Expire queued raises even when the camera stops returning frames."""
        with self._lock:
            seats, session = list(self.seats), self.session_id
            reset = self._pending_session_reset
            self._pending_session_reset = False
        if reset:
            self.state_machine.reset_all()
            self.question_order.clear()
            self.gesture_filter.clear()
            if self.face_verifier:
                self.face_verifier.clear()
        self._verification_for_seat.clear()
        if session:
            missing = KinematicEvaluation("IDLE", "NO_PERSON", 0.0, None, False)
            now_ms = self._event_time_ms(time.monotonic())
            for seat in seats:
                if seat.is_present and seat.student_id:
                    record = self.pose_engine.seat_tracks.get(seat.id)
                    if record:
                        self.gesture_filter.update(record["track_id"], missing, time.monotonic())
                    self.state_machine.process_seat_frame(seat.id, seat.student_name, missing,
                                                          session_id=session, now_ms=now_ms,
                                                          clock_ms=int(time.monotonic() * 1000))

    def _run_loop(self) -> None:
        cap = self._open_capture()
        last_reconnect_attempt = time.monotonic()
        consecutive_read_failures = 0

        while self._running:
            loop_start = time.monotonic()

            with self._lock:
                if self._restart_camera_requested:
                    self._restart_camera_requested = False
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                        cap = None
                    last_reconnect_attempt = 0.0

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
                    self._process_missing_frame()
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

                    if self.on_raw_frame:
                        self.on_raw_frame(frame)
                    elif self.on_frame:
                        encode_ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if encode_ok:
                            self.on_frame(buffer.tobytes())

                    elapsed = time.monotonic() - loop_start
                    sleep_time = self.frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue

            annotated = self._process_frame(frame)

            # 6. Emit frame callback (prefer raw ndarray for zero-copy local display)
            if self.on_raw_frame:
                self.on_raw_frame(annotated)
            elif self.on_frame:
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
