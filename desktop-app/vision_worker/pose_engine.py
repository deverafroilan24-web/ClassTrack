import cv2
import numpy as np
import torch
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ultralytics import YOLO

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


class YOLOPoseEngine:
    """
    Single-pass GPU Pose Estimation and Seat Containment Engine using YOLOv8-Pose.
    Supports Adaptive Centroid Tracking and Dynamic Person-Anchored Bounding Boxes.
    """

    def __init__(
        self,
        model_path: str = "yolov8n-pose.pt",
        confidence: float = 0.50,
        device: Optional[str] = None,
    ):
        self.confidence = confidence
        self.seat_tracks: Dict[str, dict] = {}  # seat_id -> tracking data
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

    def _is_plausible_person(self, pose: SkeletonPose, bbox: Tuple[int, int, int, int]) -> bool:
        """
        Validates that a detection corresponds to an actual human student rather than
        inanimate background items (hanging jackets, backpacks on hooks, chairs).
        Requires:
        1. Minimum bounding box dimensions (avoids tiny noise artifacts).
        2. Detectable human face/nose with confidence >= 0.35.
        3. Detectable shoulder with confidence >= 0.35.
        """
        x1, y1, x2, y2 = bbox
        bw = x2 - x1
        bh = y2 - y1
        if bw < 50 or bh < 70:
            return False

        # Real students facing camera or in classroom must have detectable head/nose
        has_head = pose.nose is not None and pose.nose.confidence >= 0.35
        if not has_head:
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
            if conf < 0.30:
                return None
            # Normalized coordinates [0.0, 1.0]
            norm_x = max(0.0, min(1.0, px / frame_w)) if frame_w > 0 else 0.0
            norm_y = max(0.0, min(1.0, py / frame_h)) if frame_h > 0 else 0.0
            return Point2D(x=norm_x, y=norm_y, confidence=conf)

        return SkeletonPose(
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
    ) -> Tuple[Dict[str, Tuple[SeatZone, Optional[PersonDetection], KinematicEvaluation]], List[PersonDetection]]:
        """
        Matches each seat to a detected person using adaptive containment and tracking.
        Smooths coordinates with an Exponential Moving Average (EMA) to prevent jitter.
        Returns: (matches_dict, unassigned_detections_list)
        """
        matches: Dict[str, Tuple[SeatZone, Optional[PersonDetection], KinematicEvaluation]] = {}
        unmatched_dets = list(detections)
        max_track_dist = max(250.0, float(frame_w) * 0.40)

        for seat in seats:
            matches[seat.id] = (
                seat,
                None,
                KinematicEvaluation(
                    status="IDLE",
                    reason_code="NO_PERSON",
                    arm_angle_deg=0.0,
                    active_arm=None,
                    is_raised_candidate=False,
                ),
            )

            if not seat.is_present:
                self.seat_tracks.pop(seat.id, None)
                continue

            # 1. Best match by direct centroid containment or high box overlap
            best_det = None
            best_score = 0.0

            for det in unmatched_dets:
                xc, yc = det.centroid
                in_containment = seat.contains_point(xc, yc, frame_w, frame_h)
                overlap = seat.compute_overlap_ratio(det.bbox, frame_w, frame_h)

                score = 1.0 if in_containment else overlap
                if score > 0.20 and score > best_score:
                    best_score = score
                    best_det = det

            # 2. Adaptive tracking fallback: follow student as they shift or move across frame
            if best_det is None and seat.id in self.seat_tracks:
                track = self.seat_tracks[seat.id]
                if track.get("frames_unseen", 0) < 30:
                    prev_xc, prev_yc = track["centroid"]
                    closest_dist = float("inf")
                    for det in unmatched_dets:
                        xc, yc = det.centroid
                        dist = np.hypot(xc - prev_xc, yc - prev_yc)
                        if dist < max_track_dist and dist < closest_dist:
                            closest_dist = dist
                            best_det = det

            if best_det is not None:
                unmatched_dets.remove(best_det)
                eval_result = evaluate_pose_kinematics(best_det.pose)

                # Smooth bounding box with Exponential Moving Average (EMA)
                bx1, by1, bx2, by2 = best_det.bbox
                if seat.id in self.seat_tracks:
                    prev_box = self.seat_tracks[seat.id]["bbox"]
                    alpha = 0.60
                    sx1 = int(alpha * bx1 + (1.0 - alpha) * prev_box[0])
                    sy1 = int(alpha * by1 + (1.0 - alpha) * prev_box[1])
                    sx2 = int(alpha * bx2 + (1.0 - alpha) * prev_box[2])
                    sy2 = int(alpha * by2 + (1.0 - alpha) * prev_box[3])
                    smoothed_box = (sx1, sy1, sx2, sy2)
                else:
                    smoothed_box = (bx1, by1, bx2, by2)

                self.seat_tracks[seat.id] = {
                    "bbox": smoothed_box,
                    "centroid": best_det.centroid,
                    "frames_unseen": 0,
                }

                smoothed_det = PersonDetection(
                    bbox=smoothed_box,
                    centroid=best_det.centroid,
                    pose=best_det.pose,
                    confidence=best_det.confidence,
                )
                matches[seat.id] = (seat, smoothed_det, eval_result)
            else:
                if seat.id in self.seat_tracks:
                    self.seat_tracks[seat.id]["frames_unseen"] += 1
                    if self.seat_tracks[seat.id]["frames_unseen"] > 30:
                        self.seat_tracks.pop(seat.id, None)

        # 3. Automatic Person-to-Seat Binding: If a person is in the frame and there is an unassigned seat,
        # bind them immediately so the seat box always locks onto the person dynamically!
        unmatched_seats = [seat for seat in seats if matches[seat.id][1] is None and seat.is_present]
        for seat in unmatched_seats:
            if not unmatched_dets:
                break
            det = unmatched_dets.pop(0)
            eval_result = evaluate_pose_kinematics(det.pose)
            bx1, by1, bx2, by2 = det.bbox

            if seat.id in self.seat_tracks:
                prev_box = self.seat_tracks[seat.id]["bbox"]
                alpha = 0.60
                sx1 = int(alpha * bx1 + (1.0 - alpha) * prev_box[0])
                sy1 = int(alpha * by1 + (1.0 - alpha) * prev_box[1])
                sx2 = int(alpha * bx2 + (1.0 - alpha) * prev_box[2])
                sy2 = int(alpha * by2 + (1.0 - alpha) * prev_box[3])
                smoothed_box = (sx1, sy1, sx2, sy2)
            else:
                smoothed_box = (bx1, by1, bx2, by2)

            self.seat_tracks[seat.id] = {
                "bbox": smoothed_box,
                "centroid": det.centroid,
                "frames_unseen": 0,
            }
            smoothed_det = PersonDetection(
                bbox=smoothed_box,
                centroid=det.centroid,
                pose=det.pose,
                confidence=det.confidence,
            )
            matches[seat.id] = (seat, smoothed_det, eval_result)

        return matches, unmatched_dets

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
        - Clean green box on hand raise, neutral clean box when seated.
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
            bx1 = max(2, x)
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

            if is_empty_seat:
                box_color = (120, 120, 130)   # Dimmed Slate Grey
                pill_bg = (60, 60, 65)
                status_text = f"{seat.student_name or 'Empty Desk'}"
                thickness = 1
            elif not is_session_active:
                box_color = (230, 175, 40)   # Clean Neutral Blue
                pill_bg = (140, 95, 20)
                status_text = f"{seat.student_name} | Standby"
                thickness = 2
            else:
                # Determine participation status & color for dynamic seat box
                is_valid_raise = eval_res and eval_res.reason_code == "VALID_HAND_RAISE"
                is_double_hand = eval_res and eval_res.reason_code == "ERR_DOUBLE_HAND_RAISE"
                is_seat_mismatch = eval_res and eval_res.reason_code == "SEAT_MISMATCH"

                if is_seat_mismatch:
                    box_color = (0, 0, 240)      # Vivid Crimson Red
                    pill_bg = (0, 0, 180)
                    status_text = f"{seat.student_name} | Wrong Seat"
                    thickness = 3
                elif is_valid_raise:
                    box_color = (0, 220, 0)      # Vivid Emerald Green
                    pill_bg = (0, 150, 0)
                    status_text = f"{seat.student_name} | HAND RAISED"
                    thickness = 3
                elif is_double_hand:
                    box_color = (0, 140, 255)    # Warning Amber / Orange
                    pill_bg = (0, 100, 200)
                    status_text = f"{seat.student_name} | Double Hand Raise"
                    thickness = 3
                else:
                    box_color = (230, 175, 40)   # Clean Neutral Blue
                    pill_bg = (140, 95, 20)
                    status_text = f"{seat.student_name} | Standby"
                    thickness = 2

            # Draw DYNAMIC Seat Bounding Box directly around the student
            draw_corner_rect(overlay, (bx1, by1), (bx2, by2), box_color, thickness=thickness)

            # Draw Header Pill Tag tracking the student's head
            draw_label_pill(overlay, status_text, bx1, by1 - 4, pill_bg)

            # Draw Skeleton if enabled
            if show_skeleton:
                draw_skeleton(pose)

        # 2. Render Any Unassigned Students Dynamically
        if unassigned_detections:
            for u_det in unassigned_detections:
                if u_det.confidence >= 0.52 and self._is_plausible_person(u_det.pose, u_det.bbox):
                    ubx1, uby1, ubx2, uby2 = u_det.bbox
                    u_eval = evaluate_pose_kinematics(u_det.pose)

                    if u_eval.reason_code == "VALID_HAND_RAISE":
                        u_box_color = (0, 220, 0)
                        u_pill_bg = (0, 150, 0)
                        u_label = "Student | HAND RAISED"
                        u_thick = 3
                    elif u_eval.reason_code == "ERR_DOUBLE_HAND_RAISE":
                        u_box_color = (0, 140, 255)
                        u_pill_bg = (0, 100, 200)
                        u_label = "Student | Double Hand Raise"
                        u_thick = 3
                    else:
                        u_box_color = (230, 175, 40)
                        u_pill_bg = (140, 95, 20)
                        u_label = "Student | Standby"
                        u_thick = 2

                    draw_corner_rect(overlay, (ubx1, uby1), (ubx2, uby2), u_box_color, thickness=u_thick)
                    draw_label_pill(overlay, u_label, ubx1, uby1 - 4, u_pill_bg)
                    if show_skeleton:
                        draw_skeleton(u_det.pose)

        return overlay
