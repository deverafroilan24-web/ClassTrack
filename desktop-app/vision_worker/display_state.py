"""One immutable per-person state supplies both the label and indicator colour."""

from dataclasses import dataclass
from typing import Optional

from vision_worker.kinematics import KinematicEvaluation


BLUE = (230, 175, 40)  # OpenCV uses BGR.
GREEN = (0, 220, 0)


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


@dataclass(frozen=True)
class PersonDisplayState:
    status: str
    label: str
    box_color: tuple
    pill_color: tuple
    thickness: int


def person_display_state(name: str, evaluation: Optional[KinematicEvaluation],
                         priority_rank: Optional[int] = None, *, assigned=True,
                         present=True, session_active=True, empty=False):
    if empty:
        return PersonDisplayState("EMPTY", name or "Empty Desk", (120, 120, 130), (60, 60, 65), 1)
    if not session_active or not present:
        return PersonDisplayState("STANDBY", f"{name} | Standby", BLUE, (140, 95, 20), 2)
    reason = evaluation.reason_code if evaluation else "NO_RAISE"
    if reason == "SEAT_MISMATCH":
        return PersonDisplayState("MISMATCH", f"{name} | Wrong Seat", (0, 0, 240), (0, 0, 180), 3)
    if reason == "ERR_DOUBLE_HAND_RAISE":
        return PersonDisplayState("DOUBLE_HAND", f"{name} | Double Hand Raise", (0, 140, 255), (0, 100, 200), 3)
    if evaluation and evaluation.is_raised_candidate and reason == "VALID_HAND_RAISE":
        # An unassigned detection can never acquire priority over an enrolled track.
        first = assigned and priority_rank == 1
        status = ("HAND RAISED" if first else f"{ordinal(priority_rank)} Hand Raised"
                  if assigned and priority_rank is not None else "Hand Raised (Unassigned)")
        return PersonDisplayState(status, f"{name} | {status}", GREEN if first else BLUE,
                                  (0, 150, 0) if first else (140, 95, 20), 3)
    return PersonDisplayState("STANDBY", f"{name} | Standby", BLUE, (140, 95, 20), 2)
