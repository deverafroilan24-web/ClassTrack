"""Slow, absent or mismatched faces must not stall video or poison identities."""

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker.face_verifier import FaceVerifier
from vision_worker.face_engine import FaceEngine
from vision_worker.kinematics import Point2D, SkeletonPose, KinematicEvaluation
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.state_machine import HandRaiseStateMachine


def inputs():
    seat = SimpleNamespace(id='a', face_embedding=json.dumps([1.0, 0.0]))
    det = SimpleNamespace(track_id=1, bbox=(0, 0, 200, 450),
                          pose=SkeletonPose(nose=Point2D(.3, .3),
                                            left_shoulder=Point2D(.25, .5), right_shoulder=Point2D(.35, .5)))
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    return seat, det, frame


def wait_result(verifier, seat, det):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = verifier.get(seat, det, time.monotonic())
        if result:
            return result
        time.sleep(.005)
    pytest.fail('Face verifier did not return a result')


def test_slow_verification_does_not_block_request_and_old_track_cannot_inherit_result():
    entered, release = threading.Event(), threading.Event()

    class Engine:
        json_to_embedding = staticmethod(FaceEngine.json_to_embedding)

        def extract_face_and_embedding(self, crop, reference_point=None):
            entered.set()
            assert release.wait(2)
            assert reference_point is not None
            return crop, np.array([1.0, 0.0])

        def verify_match(self, live, enrolled):
            return True, .9

    verifier = FaceVerifier(Engine())
    seat, det, frame = inputs()
    try:
        # The request returns even though the model is blocked until released.
        verifier.request(seat, det, frame, time.monotonic())
        assert entered.wait(1)
        assert verifier.get(seat, det, time.monotonic()) is None
        assert len(verifier._pending) == 0
        release.set()
        assert wait_result(verifier, seat, det).match is True
        det.track_id = 2
        assert verifier.get(seat, det, time.monotonic()) is None
    finally:
        release.set()
        verifier.stop()


def test_missing_face_is_unknown_and_is_retried_instead_of_cached_forever():
    class Engine:
        json_to_embedding = staticmethod(FaceEngine.json_to_embedding)

        def __init__(self):
            self.calls = 0

        def extract_face_and_embedding(self, crop, reference_point=None):
            self.calls += 1
            return None if self.calls == 1 else (crop, np.array([1.0, 0.0]))

        def verify_match(self, live, enrolled):
            return True, .9

    engine = Engine()
    verifier = FaceVerifier(engine, interval_seconds=0)
    seat, det, frame = inputs()
    try:
        verifier.request(seat, det, frame, time.monotonic())
        assert wait_result(verifier, seat, det).match is None
        verifier.request(seat, det, frame, time.monotonic())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = verifier.get(seat, det, time.monotonic())
            if result is not None and result.match is True:
                break
            time.sleep(.005)
        assert result is not None and result.match is True
        assert engine.calls == 2
    finally:
        verifier.stop()


def test_reset_discards_in_flight_result():
    entered, release = threading.Event(), threading.Event()

    class Engine:
        json_to_embedding = staticmethod(FaceEngine.json_to_embedding)

        def extract_face_and_embedding(self, crop, reference_point=None):
            entered.set()
            release.wait(2)
            return None

    verifier = FaceVerifier(Engine())
    seat, det, frame = inputs()
    try:
        verifier.request(seat, det, frame, time.monotonic())
        assert entered.wait(1)
        verifier.clear()
        release.set()
        verifier.stop()
        assert not verifier._results
    finally:
        release.set()
        verifier.stop()


def test_verification_crop_follows_nose_when_raised_hand_extends_box_upward():
    seat, det, frame = inputs()
    frame[140:150, 185:200] = (0, 255, 0)
    crop, point = FaceVerifier._face_crop(frame, det)
    assert crop is not None
    assert 0 < point[0] < crop.shape[1]
    assert 0 < point[1] < crop.shape[0]
    assert crop[:, :, 1].max() == 255


def test_detector_chooses_own_face_even_if_neighbour_has_higher_confidence():
    own = np.array([10, 10, 40, 50] + [0] * 10 + [.75], dtype=float)
    neighbour = np.array([100, 10, 40, 50] + [0] * 10 + [.99], dtype=float)
    selected = []

    class Detector:
        def setInputSize(self, size):
            pass

        def detect(self, image):
            return 1, np.array([neighbour, own])

    class Recognizer:
        def alignCrop(self, image, face):
            selected.append(face)
            return image

        def feature(self, crop):
            return np.array([[1.0, 0.0]], dtype=np.float32)

    engine = object.__new__(FaceEngine)
    engine.detector, engine.recognizer, engine.score_threshold = Detector(), Recognizer(), .6
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    assert engine.extract_face_and_embedding(frame, reference_point=(30, 35)) is not None
    assert selected[0][0] == 10
    assert engine.extract_face_and_embedding(frame, reference_point=(180, 90)) is None


def test_raise_does_not_confirm_until_initial_verification_finishes():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    valid = KinematicEvaluation('VALID', 'VALID_HAND_RAISE', 170, 'left', True)
    for now in (0, 100, 200, 300):
        machine.process_seat_frame('a', 'Alice', valid, now_ms=now, allow_confirmation=False)
    assert not events
    machine.process_seat_frame('a', 'Alice', valid, now_ms=310, allow_confirmation=True)
    assert len(events) == 1 and events[0].status == 'VALID'


def test_face_mismatch_rejects_pending_raise_and_leaves_queue_empty():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    valid = KinematicEvaluation('VALID', 'VALID_HAND_RAISE', 170, 'left', True)
    machine.process_seat_frame('a', 'Alice', valid, now_ms=0, allow_confirmation=False)
    mismatch = KinematicEvaluation('INVALID', 'SEAT_MISMATCH', 170, 'left', False)
    machine.process_seat_frame('a', 'Alice', mismatch, now_ms=300)
    assert len(events) == 1 and events[0].reason_code == 'SEAT_MISMATCH'
    assert not queue.get_podium()
