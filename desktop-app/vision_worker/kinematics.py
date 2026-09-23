import math
from dataclasses import dataclass
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

    el_dist = abs(elbow.x - head_x)
    sh_dist = abs(shoulder.x - head_x)
    wr_dist = abs(wrist.x - head_x)

    # Elbow flared lateral to head/shoulder or elevated near/above shoulder
    is_elbow_flared_or_up = (el_dist > sh_dist - 0.02) or (el_dist > 0.12) or (elbow.y < shoulder.y + 0.06)
    # Wrist tucked inward towards the head/neck centerline
    is_wrist_at_neck_center = (wr_dist < el_dist - 0.03) or (wr_dist < 0.13)
    # Wrist is near head/neck/upper chest level (NOT extended high above head in the air)
    is_wrist_near_neck_level = wrist.y >= (head_y - 0.05)

    vert_forearm = abs(elbow.y - wrist.y)
    horiz_forearm = abs(elbow.x - wrist.x)
    is_inward_or_horizontal = horiz_forearm >= (vert_forearm * 0.50) or (wr_dist < 0.09)

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
    is_wrist_above_elbow = vert_forearm_diff > 0.04

    # 2. Wrist must be distinctly elevated above the shoulder line (not down at mid-chest)
    vert_shoulder_diff = shoulder.y - wrist.y  # positive when wrist is above shoulder
    is_wrist_above_shoulder = vert_shoulder_diff > 0.05

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
    """
    Evaluates biomechanical kinematic rules on a detected skeleton pose:
    1. Check for double hand raise: both arms lifted -> ERR_DOUBLE_HAND_RAISE ("Two hands raised, not Counted").
    2. Check single hand raise:
       - Single arm lifted with forearm upright & valid elbow angle (55° - 180°) -> VALID_HAND_RAISE.
    3. Normal seated / idle posture -> NO_RAISE.
    """
    # Detect head boundary using available landmarks (eyes, ears, nose)
    head_pts = [
        p for p in [pose.left_eye, pose.right_eye, pose.left_ear, pose.right_ear, pose.nose]
        if p is not None and p.confidence >= min_confidence
    ]
    if head_pts:
        facial_y = min(p.y for p in head_pts)
        head_x = sum(p.x for p in head_pts) / len(head_pts)
    elif pose.nose and pose.nose.confidence >= min_confidence:
        facial_y = pose.nose.y
        head_x = pose.nose.x
    elif pose.left_shoulder or pose.right_shoulder:
        sh_pts = [p for p in [pose.left_shoulder, pose.right_shoulder] if p is not None and p.confidence >= min_confidence]
        facial_y = (min(p.y for p in sh_pts) - 0.12) if sh_pts else 0.3
        head_x = (sum(p.x for p in sh_pts) / len(sh_pts)) if sh_pts else 0.5
    else:
        facial_y = 0.3
        head_x = 0.5

    # True top of head / crown:
    # Facial landmarks (eyes/ears/nose) sit in the middle/lower part of the head.
    # The actual crown of the head / top of hair extends significantly higher (by ~25-35% of torso-to-face distance).
    # To count as a literal Hand Raise, the wrist must be higher than the true top of the head crown.
    sh_pts = [p for p in [pose.left_shoulder, pose.right_shoulder] if p is not None and p.confidence >= min_confidence]
    if sh_pts:
        sh_avg_y = sum(p.y for p in sh_pts) / len(sh_pts)
        head_clearance = max(0.065, (sh_avg_y - facial_y) * 0.32)
    else:
        head_clearance = 0.065

    head_y = facial_y - head_clearance

    # 1. Check if EITHER arm is in a behind-neck / behind-head resting posture:
    # A student with one hand resting/clasped behind their neck or head is lounging, resting, or stretching.
    left_behind = is_arm_resting_behind_neck(
        pose.left_shoulder, pose.left_elbow, pose.left_wrist, head_x, head_y, min_confidence
    )
    right_behind = is_arm_resting_behind_neck(
        pose.right_shoulder, pose.right_elbow, pose.right_wrist, head_x, head_y, min_confidence
    )
    if left_behind or right_behind:
        return KinematicEvaluation(
            status="IDLE",
            reason_code="NO_RAISE",
            arm_angle_deg=0.0,
            active_arm=None,
            is_raised_candidate=False,
        )

    # 2. Dual Elevated Elbows Check (Lounging / Hands-Behind-Head / Stretching posture):
    # In a legitimate single hand raise, exactly ONE arm is elevated while the other arm rests down.
    # If BOTH elbows are elevated near or above shoulder level, this is only a gesture if BOTH hands are
    # genuinely raised high in the air (ERR_DOUBLE_HAND_RAISE); otherwise it is a lounging/resting posture.
    sh_l, sh_r = pose.left_shoulder, pose.right_shoulder
    el_l, el_r = pose.left_elbow, pose.right_elbow
    wr_l, wr_r = pose.left_wrist, pose.right_wrist

    has_both_arms = (
        sh_l and sh_r and el_l and el_r
        and sh_l.confidence >= min_confidence
        and sh_r.confidence >= min_confidence
        and el_l.confidence >= min_confidence
        and el_r.confidence >= min_confidence
    )

    if has_both_arms:
        left_el_up = el_l.y < sh_l.y + 0.06
        right_el_up = el_r.y < sh_r.y + 0.06
        if left_el_up and right_el_up:
            # Check if this is a genuine double hand raise held high into the air:
            both_wrists_valid = (
                wr_l and wr_r
                and wr_l.confidence >= min_confidence
                and wr_r.confidence >= min_confidence
            )
            if both_wrists_valid:
                lw_high = wr_l.y < head_y - 0.02
                rw_high = wr_r.y < head_y - 0.02
                lw_above_el = wr_l.y < el_l.y - 0.03
                rw_above_el = wr_r.y < el_r.y - 0.03
                wrists_separated = abs(wr_l.x - wr_r.x) > 0.12
                not_at_neck = abs(wr_l.x - head_x) > 0.08 and abs(wr_r.x - head_x) > 0.08
                angle_l = calculate_joint_angle(sh_l, el_l, wr_l)
                angle_r = calculate_joint_angle(sh_r, el_r, wr_r)
                angles_open = angle_l >= 60.0 and angle_r >= 60.0

                if lw_high and rw_high and lw_above_el and rw_above_el and wrists_separated and not_at_neck and angles_open:
                    avg_angle = (angle_l + angle_r) / 2.0
                    return KinematicEvaluation(
                        status="INVALID",
                        reason_code="ERR_DOUBLE_HAND_RAISE",
                        arm_angle_deg=round(avg_angle, 1),
                        active_arm="both",
                        is_raised_candidate=False,
                    )
            # Both elbows elevated but not meeting strict double raise criteria -> Lounging/Resting posture
            return KinematicEvaluation(
                status="IDLE",
                reason_code="NO_RAISE",
                arm_angle_deg=0.0,
                active_arm=None,
                is_raised_candidate=False,
            )

    left_arm = evaluate_arm(
        pose.left_shoulder,
        pose.left_elbow,
        pose.left_wrist,
        nose=pose.nose,
        head_y=head_y,
        head_x=head_x,
        min_confidence=min_confidence,
    )
    right_arm = evaluate_arm(
        pose.right_shoulder,
        pose.right_elbow,
        pose.right_wrist,
        nose=pose.nose,
        head_y=head_y,
        head_x=head_x,
        min_confidence=min_confidence,
    )

    left_lifted = left_arm is not None and left_arm.is_lifted
    right_lifted = right_arm is not None and right_arm.is_lifted

    # Rule 1: ERR_DOUBLE_HAND_RAISE
    # Both arms lifted -> Invalid double hand gesture ("Two hands raised, not Counted")
    if left_lifted and right_lifted:
        avg_angle = (left_arm.angle_deg + right_arm.angle_deg) / 2.0
        return KinematicEvaluation(
            status="INVALID",
            reason_code="ERR_DOUBLE_HAND_RAISE",
            arm_angle_deg=round(avg_angle, 1),
            active_arm="both",
            is_raised_candidate=False,
            left_kinematics=left_arm,
            right_kinematics=right_arm,
        )

    # Determine which single arm is lifted
    active_arm_eval: Optional[ArmKinematics] = None
    active_arm_name: Optional[str] = None

    if left_lifted:
        active_arm_eval = left_arm
        active_arm_name = "left"
    elif right_lifted:
        active_arm_eval = right_arm
        active_arm_name = "right"

    # If neither arm is lifted, normal seated posture
    if active_arm_eval is None:
        return KinematicEvaluation(
            status="IDLE",
            reason_code="NO_RAISE",
            arm_angle_deg=0.0,
            active_arm=None,
            is_raised_candidate=False,
            left_kinematics=left_arm,
            right_kinematics=right_arm,
        )

    # Valid hand raise: Single arm lifted with natural angle (55° - 180°)
    if active_arm_eval.is_valid_angle:
        return KinematicEvaluation(
            status="VALID",
            reason_code="VALID_HAND_RAISE",
            arm_angle_deg=active_arm_eval.angle_deg,
            active_arm=active_arm_name,
            is_raised_candidate=True,
            left_kinematics=left_arm,
            right_kinematics=right_arm,
        )

    # If lifted but acute angle (< 50 deg, e.g. scratching head or chin rest):
    return KinematicEvaluation(
        status="IDLE",
        reason_code="NO_RAISE",
        arm_angle_deg=active_arm_eval.angle_deg,
        active_arm=active_arm_name,
        is_raised_candidate=False,
        left_kinematics=left_arm,
        right_kinematics=right_arm,
    )
