import math
from dataclasses import dataclass, replace
from typing import Optional, Tuple


MIN_VALID_ANGLE = 55.0
MAX_VALID_ANGLE = 180.0
ACUTE_ANGLE_THRESHOLD = 50.0


@dataclass(frozen=True)
class Point2D:
    x: float
    y: float
    confidence: float = 1.0


@dataclass(frozen=True)
class SkeletonPose:
    """17 COCO keypoints or normalized body landmarks."""
    nose: Optional[Point2D] = None
    left_eye: Optional[Point2D] = None
    right_eye: Optional[Point2D] = None
    left_ear: Optional[Point2D] = None
    right_ear: Optional[Point2D] = None
    left_shoulder: Optional[Point2D] = None
    right_shoulder: Optional[Point2D] = None
    left_elbow: Optional[Point2D] = None
    right_elbow: Optional[Point2D] = None
    left_wrist: Optional[Point2D] = None
    right_wrist: Optional[Point2D] = None
    left_hip: Optional[Point2D] = None
    right_hip: Optional[Point2D] = None
    image_aspect_ratio: float = 1.0


@dataclass(frozen=True)
class ArmKinematics:
    is_elevated: bool
    angle_deg: float
    wrist_y: float
    nose_y: float
    is_acute: bool
    is_valid_angle: bool
    is_above_head: bool = False
    is_lifted: bool = False
    head_y: float = 0.0


@dataclass(frozen=True)
class KinematicEvaluation:
    status: str  # "VALID" | "INVALID" | "IDLE"
    reason_code: str  # "VALID_HAND_RAISE" | "ERR_DOUBLE_HAND_RAISE" | "ERR_ELBOW_ACUTE_ANGLE" | "ERR_INSUFFICIENT_DURATION" | "NO_RAISE"
    arm_angle_deg: float
    active_arm: Optional[str]  # "left" | "right" | "both" | None
    is_raised_candidate: bool
    left_kinematics: Optional[ArmKinematics] = None
    right_kinematics: Optional[ArmKinematics] = None


def calculate_joint_angle(p_shoulder: Point2D, p_elbow: Point2D, p_wrist: Point2D) -> float:
    """
    Computes angle theta at the elbow joint between vectors:
      u = P_shoulder - P_elbow
      v = P_wrist - P_elbow
      theta = arccos((u . v) / (||u|| * ||v||)) * (180 / pi)
    Returns degrees in [0.0, 180.0].
    """
    ux = p_shoulder.x - p_elbow.x
    uy = p_shoulder.y - p_elbow.y
    vx = p_wrist.x - p_elbow.x
    vy = p_wrist.y - p_elbow.y

    norm_u = math.hypot(ux, uy)
    norm_v = math.hypot(vx, vy)

    if norm_u < 1e-6 or norm_v < 1e-6:
        return 0.0

    dot_product = ux * vx + uy * vy
    cos_theta = dot_product / (norm_u * norm_v)
    # Clamp to avoid floating point precision exceeding [-1.0, 1.0]
    cos_theta = max(-1.0, min(1.0, cos_theta))

    angle_rad = math.acos(cos_theta)
    return math.degrees(angle_rad)


def is_arm_resting_behind_neck(
    shoulder: Optional[Point2D],
    elbow: Optional[Point2D],
    wrist: Optional[Point2D],
    head_x: float,
    head_y: float,
    min_confidence: float = 0.25,
) -> bool:
    """
    Determines if an arm is folded/tucked behind or around the neck/head in a resting or lounging posture.
    """
    if not (shoulder and elbow and wrist):
        return False
    if (
        shoulder.confidence < min_confidence
        or elbow.confidence < min_confidence
        or wrist.confidence < min_confidence
    ):
        return False

    scale = max(.015, abs(shoulder.y - head_y))
    el_dist = abs(elbow.x - head_x)
    sh_dist = abs(shoulder.x - head_x)
    wr_dist = abs(wrist.x - head_x)

    # Elbow flared lateral to head/shoulder or elevated near/above shoulder
    is_elbow_flared_or_up = el_dist > sh_dist - scale * .1 or elbow.y < shoulder.y + scale * .15
    # Wrist tucked inward towards the head/neck centerline
    is_wrist_at_neck_center = wr_dist < el_dist - scale * .15 or wr_dist < scale * .45
    # Wrist is near head/neck/upper chest level (NOT extended high above head in the air)
    is_wrist_near_neck_level = wrist.y >= head_y

    vert_forearm = abs(elbow.y - wrist.y)
    horiz_forearm = abs(elbow.x - wrist.x)
    is_inward_or_horizontal = horiz_forearm >= vert_forearm * .5 or wr_dist < scale * .3

    angle = calculate_joint_angle(shoulder, elbow, wrist)
    is_folded = angle < 95.0

    return (
        is_elbow_flared_or_up
        and is_wrist_at_neck_center
        and is_wrist_near_neck_level
        and (is_inward_or_horizontal or is_folded)
    )


def evaluate_arm(
    shoulder: Optional[Point2D],
    elbow: Optional[Point2D],
    wrist: Optional[Point2D],
    nose: Optional[Point2D] = None,
    head_y: Optional[float] = None,
    head_x: Optional[float] = None,
    min_confidence: float = 0.25,
) -> Optional[ArmKinematics]:
    if not (shoulder and elbow and wrist):
        return None
    if (
        shoulder.confidence < min_confidence
        or elbow.confidence < min_confidence
        or wrist.confidence < min_confidence
    ):
        return None

    ref_head_y = head_y if head_y is not None else (nose.y if nose else 0.3)
    ref_head_x = head_x if head_x is not None else (nose.x if nose else shoulder.x)

    # In normalized image coordinates [0, 1], smaller Y is higher up.
    is_above_head = wrist.y < ref_head_y
    angle = calculate_joint_angle(shoulder, elbow, wrist)
    is_acute = angle < ACUTE_ANGLE_THRESHOLD
    is_valid_angle = MIN_VALID_ANGLE <= angle <= MAX_VALID_ANGLE

    # Biomechanical Hand Raise Posture Validation:
    # 1. Wrist must be clearly higher than the elbow
    vert_forearm_diff = elbow.y - wrist.y  # positive when wrist is above elbow
    scale = max(.015, abs(shoulder.y - ref_head_y))
    is_wrist_above_elbow = vert_forearm_diff > scale * .10

    # 2. Wrist must be distinctly elevated above the shoulder line (not down at mid-chest)
    vert_shoulder_diff = shoulder.y - wrist.y  # positive when wrist is above shoulder
    is_wrist_above_shoulder = vert_shoulder_diff > scale * .15

    # 3. Forearm orientation: In a genuine hand raise, forearm extends predominantly UPWARD.
    horiz_forearm_diff = abs(wrist.x - elbow.x)

    # 4. Single-arm Hands-Behind-Head / Neck resting check:
    is_behind_neck = is_arm_resting_behind_neck(
        shoulder, elbow, wrist, ref_head_x, ref_head_y, min_confidence
    )

    # 5. Literal Hand Raise Height Requirement:
    # A true hand raise requires the hand/wrist to be strictly ABOVE the head level (wrist.y < ref_head_y).
    # Hands at, behind, or below head level (wrist.y >= ref_head_y) are seated/resting postures, NOT a hand raise.
    is_above_head = wrist.y < ref_head_y
    is_upward_forearm = vert_forearm_diff >= (horiz_forearm_diff * 0.40)
    is_sufficient_height = is_above_head and is_wrist_above_shoulder

    is_lifted = (
        is_wrist_above_elbow
        and is_wrist_above_shoulder
        and is_upward_forearm
        and is_sufficient_height
        and not is_acute
        and not is_behind_neck
    )

    return ArmKinematics(
        is_elevated=is_lifted,
        angle_deg=round(angle, 1),
        wrist_y=wrist.y,
        nose_y=ref_head_y,
        head_y=ref_head_y,
        is_acute=is_acute,
        is_valid_angle=is_valid_angle,
        is_above_head=is_above_head,
        is_lifted=is_lifted,
    )


def evaluate_pose_kinematics(pose: SkeletonPose, min_confidence: float = 0.25) -> KinematicEvaluation:
    """Evaluate each arm independently using person-relative head clearance.

    Two valid raised arms remain an invalid double raise. A resting opposite arm
    does not cancel a single raised hand, and missing joints are unknown, not down.
    """
    # X/Y must share a scale for angles and forearm direction in wide video.
    if pose.image_aspect_ratio != 1.0:
        points = {name: replace(point, x=point.x * pose.image_aspect_ratio)
                  for name, point in vars(pose).items() if isinstance(point, Point2D)}
        pose = replace(pose, **points, image_aspect_ratio=1.0)
    head_pts = [p for p in (pose.left_eye, pose.right_eye, pose.left_ear, pose.right_ear, pose.nose)
                if p is not None and p.confidence >= min_confidence]
    shoulders = [p for p in (pose.left_shoulder, pose.right_shoulder)
                 if p is not None and p.confidence >= min_confidence]
    if not head_pts or not shoulders:
        return KinematicEvaluation("IDLE", "KEYPOINTS_MISSING", 0.0, None, False)
    facial_y = min(p.y for p in head_pts)
    head_x = sum(p.x for p in head_pts) / len(head_pts)
    face_to_shoulder = max(.015, sum(p.y for p in shoulders) / len(shoulders) - facial_y)
    head_y = facial_y - face_to_shoulder * .28
    left = evaluate_arm(pose.left_shoulder, pose.left_elbow, pose.left_wrist,
                        head_y=head_y, head_x=head_x, min_confidence=min_confidence)
    right = evaluate_arm(pose.right_shoulder, pose.right_elbow, pose.right_wrist,
                         head_y=head_y, head_x=head_x, min_confidence=min_confidence)
    left_up = left is not None and left.is_lifted and left.is_valid_angle
    right_up = right is not None and right.is_lifted and right.is_valid_angle
    if left_up and right_up:
        return KinematicEvaluation("INVALID", "ERR_DOUBLE_HAND_RAISE",
                                   round((left.angle_deg + right.angle_deg) / 2, 1),
                                   "both", False, left, right)
    if left_up or right_up:
        arm, name = (left, "left") if left_up else (right, "right")
        return KinematicEvaluation("VALID", "VALID_HAND_RAISE", arm.angle_deg,
                                   name, True, left, right)
    reason = "KEYPOINTS_MISSING" if left is None or right is None else "NO_RAISE"
    return KinematicEvaluation("IDLE", reason, 0.0, None, False, left, right)
