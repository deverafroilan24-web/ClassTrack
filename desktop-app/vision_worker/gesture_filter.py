"""Short per-person hysteresis shared by the overlay and event pipeline."""

from dataclasses import dataclass
from typing import Optional

from vision_worker.kinematics import KinematicEvaluation


@dataclass
class Signal:
    raised: Optional[KinematicEvaluation] = None
    candidate_at: Optional[float] = None
    raised_since: Optional[float] = None
    candidate_samples: int = 0
    down_at: Optional[float] = None
    last_seen: float = 0.0


class GestureFilter:
    def __init__(self, rise_seconds=.03, lower_seconds=.08, missing_seconds=.20):
        self.rise_seconds = rise_seconds
        self.lower_seconds = lower_seconds
        self.missing_seconds = missing_seconds
        self.signals = {}

    def clear(self):
        self.signals.clear()

    def reset_person(self, key):
        self.signals.pop(key, None)

    def raise_started_at(self, key):
        signal = self.signals.get(key)
        if signal is None:
            return None
        return signal.raised_since if signal.raised_since is not None else signal.candidate_at

    def update(self, key, evaluation, now):
        signal = self.signals.setdefault(key, Signal())
        signal.last_seen = now
        if evaluation.status == "INVALID":
            signal.raised, signal.candidate_at, signal.down_at = None, None, None
            signal.raised_since = None
            signal.candidate_samples = 0
            return evaluation
        if evaluation.is_raised_candidate:
            signal.down_at = None
            if signal.candidate_at is None:
                signal.candidate_at, signal.candidate_samples = now, 0
            signal.candidate_samples += 1
            if signal.raised is not None or (signal.candidate_samples >= 2 and
                                            now - signal.candidate_at >= self.rise_seconds):
                signal.raised = evaluation
                if signal.raised_since is None:
                    signal.raised_since = signal.candidate_at
                return evaluation
            return KinematicEvaluation("IDLE", "RAISE_PENDING", 0.0, None, False)
        signal.candidate_at, signal.candidate_samples = None, 0
        if signal.raised is not None:
            # A confidently observed lowered active arm uses the shorter interval.
            previous_arm = signal.raised.active_arm
            observed_arm = (evaluation.left_kinematics if previous_arm == "left"
                            else evaluation.right_kinematics)
            unknown = evaluation.reason_code in {"KEYPOINTS_MISSING", "NO_PERSON"} and observed_arm is None
            delay = self.missing_seconds if unknown else self.lower_seconds
            if signal.down_at is None:
                signal.down_at = now
            if now - signal.down_at < delay:
                return signal.raised
            signal.raised = None
            signal.raised_since = None
        signal.down_at = None
        return evaluation

    def prune(self, now):
        self.signals = {key: signal for key, signal in self.signals.items()
                        if now - signal.last_seen < 5.0}
