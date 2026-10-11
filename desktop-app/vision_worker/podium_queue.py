import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class PodiumEntry:
    seat_id: str
    student_name: str
    timestamp_ms: int
    queue_position: int
    delta_ms: int
    arm_angle: float
    reason_code: str = "VALID_HAND_RAISE"


class PodiumQueueManager:
    """
    First-to-raise queue using the camera's first observed raise timestamps.
    Same-frame ties use a stable seat ID, independent of geometry or insertion order.
    Active offers retain their original question ordinals when earlier hands lower.
    """

    def __init__(self):
        # Maps seat_id -> PodiumEntry
        self._entries: Dict[str, PodiumEntry] = {}
        self._round_entries: Dict[str, PodiumEntry] = {}
        self._fixed_positions = {}

    def get_timestamp_ms(self) -> int:
        """High-precision millisecond timestamp."""
        return time.time_ns() // 1_000_000

    def register_raise(
        self,
        seat_id: str,
        student_name: str,
        arm_angle: float,
        timestamp_ms: Optional[int] = None,
        queue_position: Optional[int] = None,
        delta_ms: Optional[int] = None,
    ) -> PodiumEntry:
        """
        Registers a confirmed hand raise. If already in queue, returns current entry.
        Camera question ordinals take precedence over active-hand list positions.
        """
        if seat_id in self._entries:
            return self._entries[seat_id]

        ts = timestamp_ms if timestamp_ms is not None else self.get_timestamp_ms()

        # Place new entry temporarily
        entry = PodiumEntry(
            seat_id=seat_id,
            student_name=student_name,
            timestamp_ms=ts,
            queue_position=len(self._entries) + 1,
            delta_ms=0,
            arm_angle=arm_angle,
        )
        self._entries[seat_id] = entry
        self._round_entries[seat_id] = entry
        if queue_position is not None:
            self._fixed_positions[seat_id] = (queue_position, delta_ms or 0)
        self._recalculate()
        return self._entries[seat_id]

    def release_raise(self, seat_id: str) -> Optional[PodiumEntry]:
        """Removes a seat from the podium queue when hand is lowered."""
        removed = self._entries.pop(seat_id, None)
        # Keep the departed student's place in this question. Do not promote
        # remaining offers when a hand is lowered or temporarily disappears.
        return removed

    def get_entry(self, seat_id: str) -> Optional[PodiumEntry]:
        return self._entries.get(seat_id)

    def get_podium(self) -> List[PodiumEntry]:
        """Returns sorted list of active entries by queue position."""
        return sorted(self._entries.values(), key=lambda e: e.queue_position)

    def clear(self) -> None:
        """Clears all active podium entries (e.g. at round reset)."""
        self._entries.clear()
        self._round_entries.clear()
        self._fixed_positions.clear()

    def _recalculate(self) -> None:
        if not self._round_entries:
            return

        sorted_entries = sorted(self._round_entries.values(), key=lambda e: (e.timestamp_ms, e.seat_id))
        first_ts = sorted_entries[0].timestamp_ms

        for rank, entry in enumerate(sorted_entries, start=1):
            entry.queue_position = rank
            entry.delta_ms = max(0, entry.timestamp_ms - first_ts)
            if entry.seat_id in self._fixed_positions:
                entry.queue_position, entry.delta_ms = self._fixed_positions[entry.seat_id]
