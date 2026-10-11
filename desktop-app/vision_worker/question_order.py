"""Remember each student's offer order until everyone visibly lowers their hands."""


class QuestionRaiseOrder:
    def __init__(self, all_down_grace_seconds=.25):
        self.timestamps = {}
        self.all_down_since = None
        self.all_down_grace_seconds = all_down_grace_seconds

    @staticmethod
    def identity(seat):
        return seat.id, seat.student_id

    def clear(self):
        self.timestamps.clear()
        self.all_down_since = None

    def observe(self, seat, started_ms):
        self.timestamps.setdefault(self.identity(seat), started_ms)

    def snapshot(self, seats):
        ordered = sorted(self.timestamps, key=lambda key: (self.timestamps[key], key))
        first = self.timestamps[ordered[0]] if ordered else 0
        ranks = {key: {"queue_pos": rank, "delta_ms": self.timestamps[key] - first}
                 for rank, key in enumerate(ordered, 1)}
        return {seat.id: ranks[self.identity(seat)] for seat in seats if self.identity(seat) in ranks}

    def ready_to_reset(self, matches, unassigned, now):
        if not self.timestamps:
            return False
        # A missing person or missing wrist is not evidence that a hand is down.
        # Also wait if a new, not-yet-confirmed offer is visible in this frame.
        lowered = all(evaluation.reason_code == "NO_RAISE"
                      for _, det, evaluation in matches.values() if det is not None)
        lowered = lowered and all(det.evaluation is not None and det.evaluation.reason_code == "NO_RAISE"
                                  for det in unassigned)
        for seat_id, student_id in self.timestamps:
            match = matches.get(seat_id)
            if match is None:
                continue  # A removed roster assignment no longer participates.
            seat, det, evaluation = match
            if not seat.is_present or seat.student_id != student_id:
                continue
            if det is None or evaluation.reason_code != "NO_RAISE":
                lowered = False
        if not lowered:
            self.all_down_since = None
            return False
        if self.all_down_since is None:
            self.all_down_since = now
        return now - self.all_down_since >= self.all_down_grace_seconds
