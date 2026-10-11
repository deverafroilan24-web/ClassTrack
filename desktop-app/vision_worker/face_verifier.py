"""Bounded asynchronous face verification; no face inference on the video thread."""

import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class Verification:
    match: Optional[bool]
    similarity: float
    completed_at: float


class FaceVerifier:
    def __init__(self, engine, interval_seconds=.8, ttl_seconds=1.6):
        self.engine = engine
        self.interval_seconds = interval_seconds
        self.ttl_seconds = ttl_seconds
        self._condition = threading.Condition()
        self._pending = {}
        self._results = {}
        self._submitted_at = {}
        self._embeddings = {}
        self._thread = None
        self._running = False
        self._generation = 0

    @staticmethod
    def key(seat, det):
        return seat.id, det.track_id, seat.face_embedding

    def get(self, seat, det, now):
        key = self.key(seat, det)
        with self._condition:
            result = self._results.get(key)
        if result is not None and now - result.completed_at <= self.ttl_seconds:
            return result
        return None

    def request(self, seat, det, frame, now):
        if not seat.face_embedding or det is None:
            return
        key = self.key(seat, det)
        with self._condition:
            previous = self._submitted_at.get(key)
            if previous is not None and now - previous < self.interval_seconds:
                return
        crop, point = self._face_crop(frame, det)
        if crop is None:
            with self._condition:
                self._submitted_at[key] = now
                self._results[key] = Verification(None, 0.0, now)
            return
        with self._condition:
            self._submitted_at[key] = now
            # One pending crop per seat. New observations replace stale ones.
            self._pending[seat.id] = (key, crop, point, self._generation)
            if self._thread is None or not self._thread.is_alive():
                self._running = True
                self._thread = threading.Thread(target=self._run, name="FaceVerifier", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    @staticmethod
    def _face_crop(frame, det):
        height, width = frame.shape[:2]
        nose = det.pose.nose
        head_points = [p for p in (det.pose.left_eye, det.pose.right_eye, det.pose.left_ear,
                                   det.pose.right_ear) if p is not None]
        shoulders = [p for p in (det.pose.left_shoulder, det.pose.right_shoulder) if p is not None]
        if (nose is None and not head_points) or not shoulders:
            return None, None
        nx = (nose.x if nose else sum(p.x for p in head_points) / len(head_points)) * width
        ny = (nose.y if nose else sum(p.y for p in head_points) / len(head_points)) * height
        head_size = max(16, sum(p.y for p in shoulders) / len(shoulders) * height - ny)
        x1, x2 = max(0, int(nx - head_size * .7)), min(width, int(nx + head_size * .7))
        y1, y2 = max(0, int(ny - head_size)), min(height, int(ny + head_size * .6))
        if x2 - x1 < 20 or y2 - y1 < 20:
            return None, None
        return frame[y1:y2, x1:x2].copy(), (nx - x1, ny - y1)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or not self._running)
                if not self._running:
                    return
                seat_id = next(iter(self._pending))
                key, crop, point, generation = self._pending.pop(seat_id)
            match, similarity = None, 0.0
            try:
                encoded = key[2]
                if encoded not in self._embeddings:
                    self._embeddings[encoded] = self.engine.json_to_embedding(encoded)
                enrolled = self._embeddings[encoded]
                face = self.engine.extract_face_and_embedding(crop, reference_point=point)
                if (enrolled is not None and face is not None and np.isfinite(enrolled).all()
                        and np.isfinite(face[1]).all()
                        and enrolled.size == face[1].size):
                    match, similarity = self.engine.verify_match(face[1], enrolled)
            except Exception as error:
                print(f"[FaceVerifier] Verification unavailable: {error}")
            result = Verification(match, similarity, time.monotonic())
            with self._condition:
                if self._running and generation == self._generation:
                    self._results[key] = result

    def prune(self, keys):
        with self._condition:
            self._results = {key: value for key, value in self._results.items() if key in keys}
            self._submitted_at = {key: value for key, value in self._submitted_at.items() if key in keys}
            self._pending = {seat: job for seat, job in self._pending.items() if job[0] in keys}

    def clear(self):
        with self._condition:
            self._generation += 1
            self._results.clear()
            self._submitted_at.clear()
            self._pending.clear()
            self._embeddings.clear()

    def stop(self):
        with self._condition:
            self._running = False
            self._pending.clear()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=2)
