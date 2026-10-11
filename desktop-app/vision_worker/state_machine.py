import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from vision_worker.kinematics import KinematicEvaluation
from vision_worker.podium_queue import PodiumEntry, PodiumQueueManager


class GestureState(str, Enum):
    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"


@dataclass
class SeatStateData:
    seat_id: str
    student_name: str
    state: GestureState = GestureState.IDLE
    candidate_start_ms: Optional[int] = None
    candidate_timestamp_ms: Optional[int] = None
    confirmed_at_ms: Optional[int] = None
    last_evaluation: Optional[KinematicEvaluation] = None
    last_event_id: Optional[str] = None
    podium_entry: Optional[PodiumEntry] = None
    last_reported_reason: Optional[str] = None
    drop_unseen_ms: Optional[int] = None
    last_confirmed_released_ms: Optional[int] = None


@dataclass
class GestureEventPayload:
    event_id: str
    session_id: Optional[str]
    seat_id: str
    student_name: str
    status: str  # "VALID" | "INVALID" | "IDLE" | "REJECTED"
    reason_code: str
    arm_angle_deg: float
    duration_sec: float
    queue_pos: Optional[int]
    delta_ms: Optional[int]
    timestamp_ms: int
    earned_point: Optional[int] = None
    verification_status: str = "VERIFIED"
    face_similarity: float = 1.0


class HandRaiseStateMachine:
    """
    State machine per seat implementing:
    - Configurable sustained-pose confirmation before triggering VALID_HAND_RAISE.
    - ERR_DOUBLE_HAND_RAISE detection.
    - ERR_ELBOW_ACUTE_ANGLE detection.
    - ERR_INSUFFICIENT_DURATION emission for short aborted raises.
    - Queue registration at confirmation, ranked by the first observed raise time.
    - HAND_LOWERED event emission when hand is lowered from CONFIRMED.
    - 1.5s cooldown after release to prevent repeated counter increments.
    """

    def __init__(
        self,
        podium_queue: PodiumQueueManager,
        on_event_callback: Callable[[GestureEventPayload], None],
        debounce_duration_sec: float = 0.20,
        cooldown_sec: float = 1.5,
        drop_grace_sec: float = 0.05,
        missing_grace_sec: float = 0.5,
    ):
        self.podium_queue = podium_queue
        self.on_event_callback = on_event_callback
        self.debounce_ms = int(debounce_duration_sec * 1000)
        self.cooldown_ms = int(cooldown_sec * 1000)
        self.drop_grace_ms = int(drop_grace_sec * 1000)
        self.missing_grace_ms = int(missing_grace_sec * 1000)
        self._seat_states: Dict[str, SeatStateData] = {}
        self._event_counter = 0

    def _next_event_id(self) -> str:
        self._event_counter += 1
        return f"evt_{int(time.time() * 1000)}_{self._event_counter}"

    def get_state(self, seat_id: str, student_name: str) -> SeatStateData:
        if seat_id not in self._seat_states:
            self._seat_states[seat_id] = SeatStateData(seat_id=seat_id, student_name=student_name)
        else:
            self._seat_states[seat_id].student_name = student_name
        return self._seat_states[seat_id]

    def reset_seat(self, seat_id: str) -> None:
        self.podium_queue.release_raise(seat_id)
        self._seat_states.pop(seat_id, None)

    def reset_all(self) -> None:
        self.podium_queue.clear()
        self._seat_states.clear()

    def raise_order_key(self, seat_id: str, observed_ms: int):
        state = self._seat_states.get(seat_id)
        timestamp = state.candidate_timestamp_ms if state else None
        return (timestamp if timestamp is not None else observed_ms, seat_id)

    def has_earlier_candidate(self, seat_id: str, observed_ms: int) -> bool:
        """Hold later confirmations while an earlier candidate is being verified."""
        order = self.raise_order_key(seat_id, observed_ms)
        return any(state.seat_id != seat_id and state.state == GestureState.CANDIDATE
                   and state.candidate_timestamp_ms is not None
                   and (state.candidate_timestamp_ms, state.seat_id) < order
                   for state in self._seat_states.values())

    def pending_priority_timestamps(self):
        """Include candidate reservations through the short missing-keypoint grace."""
        return {state.seat_id: state.candidate_timestamp_ms for state in self._seat_states.values()
                if state.state == GestureState.CANDIDATE and state.candidate_timestamp_ms is not None}

    def process_seat_frame(
        self,
        seat_id: str,
        student_name: str,
        evaluation: KinematicEvaluation,
        session_id: Optional[str] = None,
        now_ms: Optional[int] = None,
        allow_confirmation: bool = True,
        clock_ms: Optional[int] = None,
        raise_started_ms: Optional[int] = None,
        raise_started_clock_ms: Optional[int] = None,
        priority_rank: Optional[int] = None,
        priority_delta_ms: Optional[int] = None,
    ) -> Optional[GestureEventPayload]:
        """
        Processes a single frame evaluation for a given seat.
        Transitions states and emits events through the callback.
        """
        event_time_ms = now_ms if now_ms is not None else (time.time_ns() // 1_000_000)
        current_time_ms = (clock_ms if clock_ms is not None else
                           now_ms if now_ms is not None else int(time.monotonic() * 1000))
        state_data = self.get_state(seat_id, student_name)
        state_data.last_evaluation = evaluation

        # Handle Immediate Edge Cases (ERR_DOUBLE_HAND_RAISE / ERR_ELBOW_ACUTE_ANGLE)
        if evaluation.status == "INVALID":
            # If was in candidate state, drop it
            if state_data.state == GestureState.CANDIDATE:
                state_data.state = GestureState.IDLE
                state_data.candidate_start_ms = None
            elif state_data.state == GestureState.CONFIRMED:
                self.podium_queue.release_raise(seat_id)
                state_data.state = GestureState.IDLE
                state_data.confirmed_at_ms = None
                state_data.last_confirmed_released_ms = current_time_ms
            state_data.candidate_start_ms = None
            state_data.candidate_timestamp_ms = None
            state_data.drop_unseen_ms = None
            state_data.podium_entry = None

            # Only emit invalid notification when reason changes or once per occurrence
            if state_data.last_reported_reason != evaluation.reason_code:
                state_data.last_reported_reason = evaluation.reason_code
                evt = GestureEventPayload(
                    event_id=self._next_event_id(),
                    session_id=session_id,
                    seat_id=seat_id,
                    student_name=student_name,
                    status="INVALID",
                    reason_code=evaluation.reason_code,
                    arm_angle_deg=evaluation.arm_angle_deg,
                    duration_sec=0.0,
                    queue_pos=None,
                    delta_ms=None,
                    timestamp_ms=event_time_ms,
                    earned_point=None,
                )
                self.on_event_callback(evt)
                return evt
            return None

        # If IDLE / NO_RAISE
        if evaluation.status == "IDLE" or not evaluation.is_raised_candidate:
            if state_data.state == GestureState.CANDIDATE:
                # Keep a short keypoint/camera gap from restarting confirmation.
                if evaluation.reason_code in {"NO_PERSON", "KEYPOINTS_MISSING"}:
                    if state_data.drop_unseen_ms is None:
                        state_data.drop_unseen_ms = current_time_ms
                    if current_time_ms - state_data.drop_unseen_ms < 120:
                        return None
                elapsed_ms = current_time_ms - (state_data.candidate_start_ms if state_data.candidate_start_ms is not None else current_time_ms)
                state_data.state = GestureState.IDLE
                state_data.candidate_start_ms = None
                state_data.candidate_timestamp_ms = None
                state_data.drop_unseen_ms = None
                state_data.last_reported_reason = "ERR_INSUFFICIENT_DURATION"

                evt = GestureEventPayload(
                    event_id=self._next_event_id(),
                    session_id=session_id,
                    seat_id=seat_id,
                    student_name=student_name,
                    status="INVALID",
                    reason_code="ERR_INSUFFICIENT_DURATION",
                    arm_angle_deg=evaluation.arm_angle_deg,
                    duration_sec=round(elapsed_ms / 1000.0, 2),
                    queue_pos=None,
                    delta_ms=None,
                    timestamp_ms=event_time_ms,
                    earned_point=None,
                )
                self.on_event_callback(evt)
                return evt

            elif state_data.state == GestureState.CONFIRMED:
                # Student lowered hand or brief keypoint flicker
                if state_data.drop_unseen_ms is None:
                    state_data.drop_unseen_ms = current_time_ms

                drop_elapsed = current_time_ms - state_data.drop_unseen_ms
                grace_ms = self.missing_grace_ms if evaluation.reason_code == "NO_PERSON" else self.drop_grace_ms
                if drop_elapsed >= grace_ms:
                    # Sustained lower: student has put their hand down!
                    total_duration = 0.0
                    if state_data.candidate_start_ms is not None:
                        total_duration = round((current_time_ms - state_data.candidate_start_ms) / 1000.0, 2)
                    self.podium_queue.release_raise(seat_id)
                    state_data.state = GestureState.IDLE
                    state_data.confirmed_at_ms = None
                    state_data.candidate_start_ms = None
                    state_data.candidate_timestamp_ms = None
                    state_data.drop_unseen_ms = None
                    state_data.last_confirmed_released_ms = current_time_ms
                    state_data.last_reported_reason = None
                    state_data.podium_entry = None

                    # Emit HAND_LOWERED event to immediately reset podium queue and ledger
                    evt = GestureEventPayload(
                        event_id=self._next_event_id(),
                        session_id=session_id,
                        seat_id=seat_id,
                        student_name=student_name,
                        status="IDLE",
                        reason_code="HAND_LOWERED",
                        arm_angle_deg=0.0,
                        duration_sec=total_duration,
                        queue_pos=None,
                        delta_ms=None,
                        timestamp_ms=event_time_ms,
                        earned_point=None,
                    )
                    self.on_event_callback(evt)
                    return evt
                return None
            else:
                if state_data.last_reported_reason in {
                    "ERR_DOUBLE_HAND_RAISE",
                    "ERR_HAND_NOT_HIGH_ENOUGH",
                    "ERR_ELBOW_ACUTE_ANGLE",
                    "SEAT_MISMATCH",
                    "ERR_INSUFFICIENT_DURATION",
                }:
                    state_data.last_reported_reason = None
                    evt = GestureEventPayload(
                        event_id=self._next_event_id(),
                        session_id=session_id,
                        seat_id=seat_id,
                        student_name=student_name,
                        status="IDLE",
                        reason_code="NO_RAISE",
                        arm_angle_deg=0.0,
                        duration_sec=0.0,
                        queue_pos=None,
                        delta_ms=None,
                        timestamp_ms=event_time_ms,
                        earned_point=None,
                    )
                    self.on_event_callback(evt)
                    return evt
                state_data.last_reported_reason = None
            return None

        # A valid single-arm pose; display state is independent of queue cooldown.
        if evaluation.is_raised_candidate:
            state_data.drop_unseen_ms = None
            if state_data.state == GestureState.CONFIRMED:
                # Arm is still raised, cancel any drop grace timer
                state_data.drop_unseen_ms = None
                podium_entry = self.podium_queue.get_entry(seat_id)
                state_data.podium_entry = podium_entry
                return None

            if state_data.state == GestureState.IDLE:
                # Check release cooldown: student must stay seated/down for cooldown_ms before another raise
                if state_data.last_confirmed_released_ms is not None:
                    since_release = current_time_ms - state_data.last_confirmed_released_ms
                    if since_release < self.cooldown_ms:
                        return None

                # Start debounce timer
                state_data.state = GestureState.CANDIDATE
                state_data.candidate_start_ms = (raise_started_clock_ms if raise_started_clock_ms is not None
                                                else current_time_ms)
                state_data.candidate_timestamp_ms = (raise_started_ms if raise_started_ms is not None
                                                    else event_time_ms)
                state_data.last_reported_reason = "CANDIDATE"
                return None

            elif state_data.state == GestureState.CANDIDATE:
                elapsed_ms = current_time_ms - (state_data.candidate_start_ms if state_data.candidate_start_ms is not None else current_time_ms)
                if elapsed_ms >= self.debounce_ms and allow_confirmation:
                    # Sustained duration threshold reached! Transition to CONFIRMED
                    state_data.state = GestureState.CONFIRMED
                    state_data.confirmed_at_ms = current_time_ms
                    state_data.drop_unseen_ms = None

                    # Register in podium queue
                    podium_entry = self.podium_queue.register_raise(
                        seat_id=seat_id,
                        student_name=student_name,
                        arm_angle=evaluation.arm_angle_deg,
                        timestamp_ms=state_data.candidate_timestamp_ms,
                        queue_position=priority_rank,
                        delta_ms=priority_delta_ms,
                    )
                    state_data.podium_entry = podium_entry
                    state_data.last_reported_reason = "VALID_HAND_RAISE"

                    duration_sec = round(elapsed_ms / 1000.0, 2)
                    event_id = self._next_event_id()
                    state_data.last_event_id = event_id

                    evt = GestureEventPayload(
                        event_id=event_id,
                        session_id=session_id,
                        seat_id=seat_id,
                        student_name=student_name,
                        status="VALID",
                        reason_code="VALID_HAND_RAISE",
                        arm_angle_deg=evaluation.arm_angle_deg,
                        duration_sec=duration_sec,
                        queue_pos=podium_entry.queue_position,
                        delta_ms=podium_entry.delta_ms,
                        timestamp_ms=state_data.candidate_timestamp_ms,
                        earned_point=None,
                    )
                    self.on_event_callback(evt)
                    return evt
                return None

        return None
