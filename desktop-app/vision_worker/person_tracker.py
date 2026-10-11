"""Persistent person tracks, independent of detector order and seat geometry.

Motion uses the torso rather than the hand-dependent person box. A small colour
histogram supplies an inexpensive appearance cue; it is not a biometric identity.
Ambiguous observations are withheld instead of transferring a student's name.
"""

import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


def assign(costs, unmatched_cost=0.95):
    """Minimum-cost one-to-one assignment with a private unmatched option per row.

    Rectangular Hungarian algorithm. Dummy columns allow any row to stay unmatched
    and avoid adding a SciPy runtime dependency to the camera installer.
    """
    costs = np.asarray(costs, dtype=float)
    if costs.ndim != 2 or not costs.shape[0] or not costs.shape[1]:
        return []
    rows, columns = costs.shape
    matrix = np.concatenate((np.nan_to_num(costs, nan=1e6, posinf=1e6),
                             np.full((rows, rows), unmatched_cost)), axis=1)
    u, v = np.zeros(rows + 1), np.zeros(columns + rows + 1)
    p, way = np.zeros(columns + rows + 1, dtype=int), np.zeros(columns + rows + 1, dtype=int)
    for i in range(1, rows + 1):
        p[0], j0 = i, 0
        minimum, used = np.full(columns + rows + 1, np.inf), np.zeros(columns + rows + 1, dtype=bool)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], np.inf, 0
            for j in range(1, columns + rows + 1):
                if not used[j]:
                    value = matrix[i0 - 1, j - 1] - u[i0] - v[j]
                    if value < minimum[j]:
                        minimum[j], way[j] = value, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(columns + rows + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return [(p[j] - 1, j - 1) for j in range(1, columns + 1)
            if p[j] and costs[p[j] - 1, j - 1] < unmatched_cost]


def box_iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return intersection / max(1, area_a + area_b - intersection)


def torso_anchor(det, width, height):
    shoulders = [p for p in (det.pose.left_shoulder, det.pose.right_shoulder)
                 if p is not None and p.confidence >= 0.3]
    if shoulders:
        center_x = (sum(p.x for p in shoulders) / len(shoulders) if len(shoulders) == 2 else
                    det.pose.nose.x if det.pose.nose is not None else det.centroid[0] / width)
        return np.array([center_x * width, sum(p.y for p in shoulders) * height / len(shoulders)])
    if det.pose.nose is not None:
        return np.array([det.pose.nose.x * width, det.pose.nose.y * height])
    return np.array(det.centroid, dtype=float)


def appearance_descriptor(frame, det, width, height):
    if frame is None:
        return None
    left, right = det.pose.left_shoulder, det.pose.right_shoulder
    if left is None or right is None:
        return None
    span = abs(left.x - right.x) * width
    if span < 8:
        return None
    cx, cy = torso_anchor(det, width, height)
    hips = [p.y * height for p in (det.pose.left_hip, det.pose.right_hip) if p is not None]
    bottom = min(hips) if hips else cy + (det.bbox[3] - cy) * 0.55
    # Central shirt patch excludes hands, face and most background pixels.
    x1, x2 = max(0, int(cx - span * .35)), min(width, int(cx + span * .35))
    y1, y2 = max(0, int(cy + (bottom - cy) * .15)), min(height, int(cy + (bottom - cy) * .65))
    if x2 - x1 < 5 or y2 - y1 < 5:
        return None
    crop = cv2.resize(frame[y1:y2, x1:x2], (24, 24), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
    # Soft bins tolerate small lighting changes instead of creating a new track
    # whenever a shirt colour moves across a histogram bin boundary.
    histogram = .6 * histogram + .2 * np.roll(histogram, 1, axis=0) + .2 * np.roll(histogram, -1, axis=0)
    for axis in (1, 2):
        padding = [(0, 0)] * 3
        padding[axis] = (1, 1)
        padded = np.pad(histogram, padding, mode="edge")
        before, after = [slice(None)] * 3, [slice(None)] * 3
        before[axis], after[axis] = slice(0, -2), slice(2, None)
        histogram = .6 * histogram + .2 * padded[tuple(before)] + .2 * padded[tuple(after)]
    histogram = histogram.flatten()
    return histogram / max(1.0, float(histogram.sum()))


def appearance_distance(a, b):
    if a is None or b is None:
        return None
    return float(np.sqrt(max(0.0, 1.0 - np.sqrt(a * b).sum())))


@dataclass
class PersonTrack:
    id: int
    bbox: tuple
    anchor: np.ndarray
    velocity: np.ndarray
    appearance: Optional[np.ndarray]
    first_seen: float
    last_seen: float
    hits: int = 1
    seat_id: Optional[str] = None
    detection: object = None

    def predict(self, now):
        return self.anchor + self.velocity * min(max(0.0, now - self.last_seen), .6)


class PersonTracker:
    def __init__(self, retention_seconds=3.0):
        self.tracks = {}
        self.next_id = 1
        self.retention_seconds = retention_seconds
        self.frame_size = None

    def reset(self):
        self.tracks.clear()
        self.frame_size = None
        # IDs are never reused, including after camera/section changes.

    def release_seat(self, seat_id):
        for track in self.tracks.values():
            if track.seat_id == seat_id:
                track.seat_id = None

    def update(self, detections, width, height, frame=None, now=None):
        now = time.monotonic() if now is None else now
        if self.frame_size is not None and self.frame_size != (width, height):
            self.reset()
        self.frame_size = (width, height)
        for track_id, track in list(self.tracks.items()):
            track.detection = None
            if now - track.last_seen > self.retention_seconds:
                del self.tracks[track_id]
        tracks = list(self.tracks.values())
        anchors = [torso_anchor(det, width, height) for det in detections]
        descriptors = [appearance_descriptor(frame, det, width, height) for det in detections]
        costs = np.full((len(tracks), len(detections)), np.inf)
        for i, track in enumerate(tracks):
            gap = now - track.last_seen
            size = max(24.0, (track.bbox[3] - track.bbox[1]) * .30)
            gate = size * min(2.5, 1.0 + gap * 2)
            for j, det in enumerate(detections):
                distance = float(np.linalg.norm(anchors[j] - track.predict(now)))
                appearance = appearance_distance(track.appearance, descriptors[j])
                if distance > gate or (appearance is not None and appearance > .70):
                    continue
                # Reappearance needs stronger evidence than uninterrupted motion.
                if gap > .6 and (appearance is None or appearance > .30) and distance > size * .3:
                    continue
                overlap = box_iou(track.bbox, det.bbox)
                costs[i, j] = .65 * distance / gate + .15 * (1 - overlap)
                if appearance is not None:
                    costs[i, j] += .55 * appearance

        blocked = set()
        for j in range(len(detections)):
            options = np.sort(costs[:, j])
            if len(options) > 1 and options[1] < .95 and options[1] - options[0] < .08:
                # Full overlap with no distinguishing observation: reserve both
                # identities and wait for separation rather than choosing by order.
                blocked.add(j)
                costs[:, j] = np.inf
        assigned = set()
        for i, j in assign(costs):
            track, det = tracks[i], detections[j]
            dt = max(.001, now - track.last_seen)
            if dt < .3:
                measured_velocity = (anchors[j] - track.anchor) / dt
                track.velocity = .6 * measured_velocity + .4 * track.velocity
            else:
                track.velocity *= .5
            track.anchor, track.bbox, track.last_seen = anchors[j], det.bbox, now
            track.hits += 1
            track.detection = det
            # Don't contaminate the appearance template during an overlap.
            crowded = any(k != j and box_iou(det.bbox, other.bbox) > .35
                          for k, other in enumerate(detections))
            if descriptors[j] is not None and not crowded:
                track.appearance = (descriptors[j] if track.appearance is None else
                                    .9 * track.appearance + .1 * descriptors[j])
            det.track_id = track.id
            assigned.add(j)
        for j, det in enumerate(detections):
            if j in assigned:
                continue
            det.track_id = None
            if j in blocked:
                continue
            track_id = self.next_id
            self.next_id += 1
            track = PersonTrack(track_id, det.bbox, anchors[j], np.zeros(2), descriptors[j], now, now,
                                detection=det)
            self.tracks[track_id] = track
            # New anonymous tracks can be observed while old name bindings remain
            # reserved. Seat binding is handled separately and never steals them.
            det.track_id = track_id
        return self.tracks
