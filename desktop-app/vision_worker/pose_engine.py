import cv2
import numpy as np
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from vision_worker.person_tracker import PersonTracker, assign
from vision_worker.display_state import person_display_state

from vision_worker.kinematics import (
    KinematicEvaluation,
    Point2D,
    SkeletonPose,
    evaluate_pose_kinematics,
)


@dataclass
class SeatZone:
    id: str
    label: str
    student_name: str
    student_id_number: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    is_present: bool = True
    student_id: Optional[str] = None
    photo_path: Optional[str] = None
    face_embedding: Optional[str] = None
    grid_row: int = 0
    grid_col: int = 0

    def contains_point(self, px: float, py: float, frame_w: int, frame_h: int) -> bool:
        """
        Checks if point (px, py) falls inside seat zone.
        Handles both normalized [0, 1] and pixel coordinates.
        """
        is_normalized = self.x_max <= 1.0 and self.y_max <= 1.0

        if is_normalized:
            norm_x = px / frame_w if frame_w > 0 else px
            norm_y = py / frame_h if frame_h > 0 else py
            return (self.x_min <= norm_x <= self.x_max) and (self.y_min <= norm_y <= self.y_max)
        else:
            return (self.x_min <= px <= self.x_max) and (self.y_min <= py <= self.y_max)

    def compute_overlap_ratio(self, bbox: Tuple[int, int, int, int], frame_w: int, frame_h: int) -> float:
        """
        Computes intersection over person bounding box area with the seat zone.
        """
        is_normalized = self.x_max <= 1.0 and self.y_max <= 1.0
        sx1 = int(self.x_min * frame_w if is_normalized else self.x_min)
        sy1 = int(self.y_min * frame_h if is_normalized else self.y_min)
        sx2 = int(self.x_max * frame_w if is_normalized else self.x_max)
        sy2 = int(self.y_max * frame_h if is_normalized else self.y_max)

        bx1, by1, bx2, by2 = bbox
        ix1 = max(sx1, bx1)
        iy1 = max(sy1, by1)
        ix2 = min(sx2, bx2)
        iy2 = min(sy2, by2)

        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0

        inter_area = (ix2 - ix1) * (iy2 - iy1)
        box_area = max(1, (bx2 - bx1) * (by2 - by1))
        return inter_area / float(box_area)


@dataclass
class PersonDetection:
    bbox: Tuple[int, int, int, int]  # x1, y1, x2, y2
    centroid: Tuple[float, float]     # xc, yc
    pose: SkeletonPose
    confidence: float
    track_id: Optional[int] = None
    evaluation: Optional[KinematicEvaluation] = None


class YOLOPoseEngine:
    """
    Single-pass pose estimation with persistent person IDs and locked seat names.
    """

    def __init__(
        self,
        model_path: str = "yolov8n-pose.pt",
        confidence: float = 0.50,
        device: Optional[str] = None,
    ):
        self.confidence = confidence
        self.seat_tracks: Dict[str, dict] = {}  # seat_id -> tracking data
        self.tracker = PersonTracker()
        from ultralytics import YOLO
        from ultralytics.cfg import DEFAULT_CFG_DICT
        import torch
        if device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device

        self.model = YOLO(model_path)
        try:
            self.model.to(self.device)
        except Exception:
            self.device = "cpu"
            self.model.to("cpu")
        # Support current and older Ultralytics releases without per-frame
        # deprecation warnings or changing the model's input resolution.
        cuda = str(self.device).startswith("cuda")
        self._precision_options = ({"quantize": 16 if cuda else 32}
                                   if "quantize" in DEFAULT_CFG_DICT else {"half": cuda})

    def _is_plausible_person(self, pose: SkeletonPose, bbox: Tuple[int, int, int, int]) -> bool:
        """
        Validates that a detection corresponds to an actual human student rather than
        inanimate background items (hanging jackets, backpacks on hooks, chairs).
        Requires:
        Requires a plausible box, a shoulder and either a visible head landmark
        or a confident torso. Side views and turned backs can retain their tracks.
        """
        x1, y1, x2, y2 = bbox
        bw = x2 - x1
        bh = y2 - y1
        if bw < 20 or bh < 35:
            return False

        # Real students facing camera or in classroom must have detectable head/nose
        has_head = any(point is not None and point.confidence >= .35 for point in
                       (pose.nose, pose.left_eye, pose.right_eye, pose.left_ear, pose.right_ear))
        has_torso = (pose.left_shoulder is not None and pose.right_shoulder is not None
                     and pose.left_shoulder.confidence >= .5 and pose.right_shoulder.confidence >= .5
                     and any(point is not None and point.confidence >= .5
                             for point in (pose.left_hip, pose.right_hip)))
        if not has_head and not has_torso:
            return False

        # Real students have at least one detectable shoulder
        has_shoulder = (
            (pose.left_shoulder is not None and pose.left_shoulder.confidence >= 0.35)
            or (pose.right_shoulder is not None and pose.right_shoulder.confidence >= 0.35)
        )
        if not has_shoulder:
            return False

        return True

    def infer_frame(self, frame: np.ndarray) -> List[PersonDetection]:
        """
        Runs single-pass pose estimation on the entire frame.
        Extracts bounding boxes, centroids, and 17 COCO keypoints for all people.
        Filters out low-confidence object false positives.
        """
        h, w = frame.shape[:2]
        results = self.model(
            frame,
            conf=self.confidence,
            device=self.device,
            verbose=False,
            imgsz=640,
            **self._precision_options,
        )

        detections: List[PersonDetection] = []
        if not results or len(results) == 0:
            return detections

        r = results[0]
        if r.boxes is None or r.keypoints is None:
            return detections

        boxes_data = r.boxes.data.cpu().numpy() if r.boxes.data is not None else []
        kpts_data = r.keypoints.data.cpu().numpy() if r.keypoints.data is not None else []

        for i, box in enumerate(boxes_data):
            x1, y1, x2, y2 = map(int, box[:4])
            conf = float(box[4]) if len(box) > 4 else 1.0

            if conf < self.confidence:
                continue

            # Extract keypoints
            pose = self._extract_pose(kpts_data[i] if i < len(kpts_data) else None, w, h)

            # Filter out non-human inanimate objects (jackets, bags, chairs)
            if not self._is_plausible_person(pose, (x1, y1, x2, y2)):
                continue

            xc = (x1 + x2) / 2.0
            yc = (y1 + y2) / 2.0

            detections.append(
                PersonDetection(
                    bbox=(x1, y1, x2, y2),
                    centroid=(xc, yc),
                    pose=pose,
                    confidence=conf,
                )
            )

        return detections

    def reset_seat(self, seat_id: str) -> None:
        self.seat_tracks.pop(seat_id, None)
        self.tracker.release_seat(seat_id)

    def reset_tracking(self) -> None:
        self.seat_tracks.clear()
        self.tracker.reset()

    def _extract_pose(self, kpt_array: Optional[np.ndarray], frame_w: int, frame_h: int) -> SkeletonPose:
        """
        Converts YOLO keypoints (17, 2 or 3) into normalized SkeletonPose.
        COCO keypoints index mapping:
          0: nose
          5: left_shoulder, 6: right_shoulder
          7: left_elbow, 8: right_elbow
          9: left_wrist, 10: right_wrist
          11: left_hip, 12: right_hip
        """
        if kpt_array is None or len(kpt_array) < 11:
            return SkeletonPose()

        def make_pt(idx: int) -> Optional[Point2D]:
            if idx >= len(kpt_array):
                return None
            pt = kpt_array[idx]
            px, py = float(pt[0]), float(pt[1])
            conf = float(pt[2]) if len(pt) > 2 else 1.0
            if conf < 0.25 or (px == 0.0 and py == 0.0):
                return None
            # Normalized coordinates [0.0, 1.0]
            norm_x = max(0.0, min(1.0, px / frame_w)) if frame_w > 0 else 0.0
            norm_y = max(0.0, min(1.0, py / frame_h)) if frame_h > 0 else 0.0
            return Point2D(x=norm_x, y=norm_y, confidence=conf)

        return SkeletonPose(
            image_aspect_ratio=frame_w / max(1, frame_h),
            nose=make_pt(0),
            left_eye=make_pt(1),
            right_eye=make_pt(2),
            left_ear=make_pt(3),
            right_ear=make_pt(4),
            left_shoulder=make_pt(5),
            right_shoulder=make_pt(6),
            left_elbow=make_pt(7),
            right_elbow=make_pt(8),
            left_wrist=make_pt(9),
            right_wrist=make_pt(10),
            left_hip=make_pt(11),
            right_hip=make_pt(12),
        )

    def match_seats(
        self,
        detections: List[PersonDetection],
        seats: List[SeatZone],
        frame_w: int,
        frame_h: int,
        frame: Optional[np.ndarray] = None,
        now: Optional[float] = None,
    ) -> Tuple[Dict[str, Tuple[SeatZone, Optional[PersonDetection], KinematicEvaluation]], List[PersonDetection]]:
        """Bind names once, then follow persistent person IDs independently of seats.

        A missing person reserves their name for the track retention period. Seat
        geometry is used only for initial binding, never to rename an active track.
        """
        now = time.monotonic() if now is None else now
        tracks = self.tracker.update(detections, frame_w, frame_h, frame=frame, now=now)
        active = {seat.id: seat for seat in seats if seat.is_present and seat.student_id
                  and seat.student_name and not seat.student_name.startswith("Empty")}
        for track in tracks.values():
            if track.seat_id not in active:
                track.seat_id = None
        reserved = {track.seat_id for track in tracks.values() if track.seat_id}
        free_seats = [seat for seat in active.values() if seat.id not in reserved]
        anonymous = [track for track in tracks.values()
                     if not track.seat_id and track.detection is not None and track.hits >= 2]
        costs = np.full((len(free_seats), len(anonymous)), np.inf)
        for i, seat in enumerate(free_seats):
            normalized = seat.x_max <= 1.0 and seat.y_max <= 1.0
            sx1, sx2 = (seat.x_min * frame_w, seat.x_max * frame_w) if normalized else (seat.x_min, seat.x_max)
            sy1, sy2 = (seat.y_min * frame_h, seat.y_max * frame_h) if normalized else (seat.y_min, seat.y_max)
            for j, track in enumerate(anonymous):
                px, py = track.anchor
                if not seat.contains_point(px, py, frame_w, frame_h):
                    continue
                distance = np.hypot((px - (sx1 + sx2) / 2) / max(1, sx2 - sx1),
                                    (py - (sy1 + sy2) / 2) / max(1, sy2 - sy1))
                overlap = seat.compute_overlap_ratio(track.bbox, frame_w, frame_h)
                costs[i, j] = .65 * distance + .35 * (1 - overlap)
        # Identical/overlapping calibration zones cannot establish a safe name.
        for j in range(len(anonymous)):
            options = np.sort(costs[:, j])
            if len(options) > 1 and options[1] < .85 and options[1] - options[0] < .06:
                costs[:, j] = np.inf
        for i, j in assign(costs, unmatched_cost=.85):
            anonymous[j].seat_id = free_seats[i].id

        matches = {seat.id: (seat, None, KinematicEvaluation("IDLE", "NO_PERSON", 0.0, None, False))
                   for seat in seats}
        assigned_ids = set()
        self.seat_tracks = {seat_id: record for seat_id, record in self.seat_tracks.items()
                            if seat_id in active and seat_id in {t.seat_id for t in tracks.values()}}
        for track in tracks.values():
            if not track.seat_id:
                continue
            seat_id, det = track.seat_id, track.detection
            old = self.seat_tracks.get(seat_id)
            if det is None:
                if old:
                    old["frames_unseen"] += 1
                continue
            smoothed_box = det.bbox
            if old and old.get("track_id") == track.id and old.get("frames_unseen", 0) == 0:
                smoothed_box = tuple(int(.85 * value + .15 * previous)
                                     for value, previous in zip(det.bbox, old["bbox"]))
            self.seat_tracks[seat_id] = {"bbox": smoothed_box, "centroid": det.centroid,
                                        "frames_unseen": 0, "track_id": track.id}
            # Keep raw geometry for kinematics, appearance and biometric cropping.
            display_det = PersonDetection(smoothed_box, det.centroid, det.pose, det.confidence, track.id)
            matches[seat_id] = (active[seat_id], display_det, evaluate_pose_kinematics(det.pose))
            assigned_ids.add(track.id)
        return matches, [det for det in detections if det.track_id not in assigned_ids]

    def render_overlay(
        self,
        frame: np.ndarray,
        seats: List[SeatZone],
        matches: Dict[str, Tuple[SeatZone, Optional[PersonDetection], KinematicEvaluation]],
        unassigned_detections: Optional[List[PersonDetection]] = None,
        podium_entries: Optional[dict] = None,
        show_skeleton: bool = False,
        is_session_active: bool = True,
    ) -> np.ndarray:
        """
        Renders clean real-time visual overlays:
        - 100% DYNAMIC Seat Bounding Boxes directly tracking the detected person.
        - Green first offer; blue 2nd Hand Raised, 3rd Hand Raised, etc.
        - Skeletons (bones & joint dots) turned OFF by default for clean classroom viewing.
        """
        overlay = frame.copy()
        h, w = frame.shape[:2]

        def draw_corner_rect(img, p1, p2, color, thickness=2, corner_len=20):
            x1, y1 = p1
            x2, y2 = p2
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)
            cl_x = min(corner_len, max(8, abs(x2 - x1) // 4))
            cl_y = min(corner_len, max(8, abs(y2 - y1) // 4))
            # Top-Left
            cv2.line(img, (x1, y1), (x1 + cl_x, y1), color, thickness)
            cv2.line(img, (x1, y1), (x1, y1 + cl_y), color, thickness)
            # Top-Right
            cv2.line(img, (x2, y1), (x2 - cl_x, y1), color, thickness)
            cv2.line(img, (x2, y1), (x2, y1 + cl_y), color, thickness)
            # Bottom-Left
            cv2.line(img, (x1, y2), (x1 + cl_x, y2), color, thickness)
            cv2.line(img, (x1, y2), (x1, y2 - cl_y), color, thickness)
            # Bottom-Right
            cv2.line(img, (x2, y2), (x2 - cl_x, y2), color, thickness)
            cv2.line(img, (x2, y2), (x2, y2 - cl_y), color, thickness)

        def draw_label_pill(img, text, x, y, bg_color, text_color=(255, 255, 255), scale=0.52):
            font = cv2.FONT_HERSHEY_SIMPLEX
            (tw, th), _ = cv2.getTextSize(text, font, scale, 1)
            pad = 5
            bx1 = min(max(2, x), max(2, img.shape[1] - tw - pad * 2 - 2))
            by1 = max(2, y - th - pad * 2)
            bx2 = min(img.shape[1] - 2, bx1 + tw + pad * 2)
            by2 = max(th + pad * 2, y)
            cv2.rectangle(img, (bx1, by1), (bx2, by2), bg_color, -1)
            cv2.putText(img, text, (bx1 + pad, by2 - pad - 1), font, scale, text_color, 1, cv2.LINE_AA)

        def draw_skeleton(pose: SkeletonPose):
            def draw_bone(p1: Optional[Point2D], p2: Optional[Point2D], bone_color=(0, 255, 255)):
                if p1 and p2:
                    cv2.line(overlay, (int(p1.x * w), int(p1.y * h)), (int(p2.x * w), int(p2.y * h)), bone_color, 2)

            draw_bone(pose.left_shoulder, pose.left_elbow)
            draw_bone(pose.left_elbow, pose.left_wrist)
            draw_bone(pose.right_shoulder, pose.right_elbow)
            draw_bone(pose.right_elbow, pose.right_wrist)
            draw_bone(pose.left_shoulder, pose.right_shoulder, (255, 120, 120))

            for pt in [pose.nose, pose.left_shoulder, pose.right_shoulder, pose.left_elbow, pose.right_elbow, pose.left_wrist, pose.right_wrist]:
                if pt:
                    cv2.circle(overlay, (int(pt.x * w), int(pt.y * h)), 4, (0, 0, 255), -1)

        # 1. Render DYNAMIC Seat Bounding Boxes Tracking Matched Students
        for seat_id, (seat, det, eval_res) in matches.items():
            if det is None:
                continue

            pose = det.pose
            bx1, by1, bx2, by2 = det.bbox

            # Check if desk is empty/unassigned
            is_empty_seat = (
                not seat.student_id
                or not seat.student_name
                or seat.student_name.startswith("Empty")
                or not seat.student_name.strip()
            )

            display = person_display_state(
                seat.student_name, eval_res,
                (podium_entries or {}).get(seat_id, {}).get("queue_pos"),
                present=seat.is_present, session_active=is_session_active, empty=is_empty_seat)

            # Draw DYNAMIC Seat Bounding Box directly around the student
            draw_corner_rect(overlay, (bx1, by1), (bx2, by2), display.box_color, thickness=display.thickness)

            # Draw Header Pill Tag tracking the student's head
            draw_label_pill(overlay, display.label, bx1, by1 - 4, display.pill_color)

            # Draw Skeleton if enabled
            if show_skeleton:
                draw_skeleton(pose)

        # 2. Render Any Unassigned Students Dynamically
        if unassigned_detections:
            for u_det in unassigned_detections:
                if u_det.confidence >= self.confidence:
                    ubx1, uby1, ubx2, uby2 = u_det.bbox
                    u_eval = u_det.evaluation or evaluate_pose_kinematics(u_det.pose)

                    display = person_display_state("Unassigned", u_eval, assigned=False,
                                                   session_active=is_session_active)
                    draw_corner_rect(overlay, (ubx1, uby1), (ubx2, uby2), display.box_color, thickness=display.thickness)
                    draw_label_pill(overlay, display.label, ubx1, uby1 - 4, display.pill_color)
                    if show_skeleton:
                        draw_skeleton(u_det.pose)

        return overlay
