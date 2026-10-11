"""Pose, timing and overlay regressions use deterministic skeletons."""

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker.gesture_filter import GestureFilter
from vision_worker.kinematics import Point2D, SkeletonPose, KinematicEvaluation, evaluate_pose_kinematics
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.state_machine import HandRaiseStateMachine
from vision_worker.pose_engine import YOLOPoseEngine, SeatZone, PersonDetection
from vision_worker.worker import VisionWorker


def pose(raised=True, both=False):
    return SkeletonPose(nose=Point2D(.5, .3),
                        left_shoulder=Point2D(.4, .5), right_shoulder=Point2D(.6, .5),
                        left_elbow=Point2D(.4, .3 if raised else .65),
                        left_wrist=Point2D(.4, .1 if raised else .8),
                        right_elbow=Point2D(.6, .3 if both else .65),
                        right_wrist=Point2D(.6, .1 if both else .8))


@pytest.mark.parametrize('scale', [1.0, .5, .2])
def test_raise_and_lower_are_independent_of_distance_from_camera(scale):
    for raised in (True, False):
        source = pose(raised)
        scaled = replace(source, **{name: Point2D(.5 + (p.x - .5) * scale, .4 + (p.y - .4) * scale)
                                    for name, p in vars(source).items() if isinstance(p, Point2D)})
        assert evaluate_pose_kinematics(scaled).is_raised_candidate is raised


def test_resting_opposite_arm_does_not_cancel_single_raise():
    resting = replace(pose(), right_elbow=Point2D(.8, .45), right_wrist=Point2D(.52, .3))
    assert evaluate_pose_kinematics(resting).reason_code == 'VALID_HAND_RAISE'
    assert evaluate_pose_kinematics(pose(True, True)).reason_code == 'ERR_DOUBLE_HAND_RAISE'
    no_raise = replace(resting, left_elbow=Point2D(.4, .65), left_wrist=Point2D(.4, .8))
    assert not evaluate_pose_kinematics(no_raise).is_raised_candidate


def test_missing_wrist_is_unknown_and_zero_coordinates_are_discarded():
    assert evaluate_pose_kinematics(replace(pose(), left_wrist=None)).reason_code == 'KEYPOINTS_MISSING'
    engine = object.__new__(YOLOPoseEngine)
    points = np.zeros((17, 3))
    points[:, 2] = .99
    assert engine._extract_pose(points, 640, 480).left_wrist is None


def test_filter_and_events_respond_quickly_without_duplicate_raises():
    raised, lowered = evaluate_pose_kinematics(pose()), evaluate_pose_kinematics(pose(False))
    filter_ = GestureFilter()
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    first_raise = None
    for i in range(30):
        now = i / 30
        filtered = filter_.update(1, raised, now)
        if filtered.is_raised_candidate and first_raise is None:
            first_raise = now
        machine.process_seat_frame('a', 'Alice', filtered, 'class', now_ms=round(now * 1000))
    assert first_raise <= .067
    confirms = [event for event in events if event.status == 'VALID']
    assert len(confirms) == 1
    assert confirms[0].timestamp_ms <= 300
    assert machine.get_state('a', 'Alice').confirmed_at_ms <= 300
    blue_at = None
    for i in range(30, 41):
        now = i / 30
        filtered = filter_.update(1, lowered, now)
        if not filtered.is_raised_candidate and blue_at is None:
            blue_at = now
        machine.process_seat_frame('a', 'Alice', filtered, 'class', now_ms=round(now * 1000))
    assert blue_at <= 1.1
    assert not queue.get_podium()
    assert len([event for event in events if event.reason_code == 'HAND_LOWERED']) == 1


def test_keypoint_flicker_keeps_raise_and_does_not_reset_candidate():
    raised = evaluate_pose_kinematics(pose())
    unknown = evaluate_pose_kinematics(replace(pose(), left_wrist=None))
    filter_ = GestureFilter()
    filter_.update(1, raised, 0)
    assert filter_.update(1, raised, .04).is_raised_candidate
    assert filter_.update(1, unknown, .08).is_raised_candidate
    assert filter_.update(1, raised, .12).is_raised_candidate
    assert filter_.update(2, unknown, .12).is_raised_candidate is False


def test_candidate_start_at_zero_confirms_and_invalid_state_clears():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    raised = evaluate_pose_kinematics(pose())
    machine.process_seat_frame('a', 'Alice', raised, now_ms=0)
    machine.process_seat_frame('a', 'Alice', raised, now_ms=200)
    assert queue.get_entry('a')
    mismatch = KinematicEvaluation('INVALID', 'SEAT_MISMATCH', 170, 'left', False)
    machine.process_seat_frame('a', 'Alice', mismatch, now_ms=300)
    assert not queue.get_entry('a')
    assert machine.get_state('a', 'Alice').drop_unseen_ms is None
    machine.process_seat_frame('a', 'Alice', evaluate_pose_kinematics(pose(False)), now_ms=400)
    assert events[-1].status == 'IDLE'
    assert events[-1].reason_code == 'NO_RAISE'


def test_clock_adjustment_does_not_delay_confirmation_or_overwrite_raise_onset():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    raised = evaluate_pose_kinematics(pose())
    machine.process_seat_frame('a', 'Alice', raised, now_ms=10_000, clock_ms=0)
    machine.process_seat_frame('a', 'Alice', raised, now_ms=5_000, clock_ms=210)
    assert events[0].timestamp_ms == 10_000
    assert events[0].duration_sec == .21
    assert queue.get_entry('a').timestamp_ms == 10_000


def test_brief_person_loss_keeps_queue_but_prolonged_loss_clears_it():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    raised = evaluate_pose_kinematics(pose())
    missing = KinematicEvaluation('IDLE', 'NO_PERSON', 0, None, False)
    for now in (0, 200):
        machine.process_seat_frame('a', 'Alice', raised, now_ms=now)
    for now in (300, 600):
        machine.process_seat_frame('a', 'Alice', missing, now_ms=now)
    assert queue.get_entry('a')
    machine.process_seat_frame('a', 'Alice', raised, now_ms=650)
    assert len([e for e in events if e.status == 'VALID']) == 1
    for now in (700, 1200):
        machine.process_seat_frame('a', 'Alice', missing, now_ms=now)
    assert not queue.get_entry('a')


def test_first_raise_is_green_and_waiting_indicators_are_blue(monkeypatch):
    engine = object.__new__(YOLOPoseEngine)
    engine.confidence = .5
    valid, down = evaluate_pose_kinematics(pose()), evaluate_pose_kinematics(pose(False))
    roster = [SeatZone(str(i), str(i), f'Student {i}', str(i), 0, 0, 1, 1, student_id=str(i))
              for i in range(2)]
    labels = []
    import cv2
    monkeypatch.setattr(cv2, 'putText', lambda image, text, *args, **kwargs: labels.append(text))
    detections = [PersonDetection((20 + i * 200, 100, 160 + i * 200, 400), (90 + i * 200, 250), pose(), .9, i)
                  for i in range(3)]
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    matches = {seat.id: (seat, detections[i], valid) for i, seat in enumerate(roster)}
    frame = engine.render_overlay(image, roster, matches, [detections[2]],
                                  podium_entries={'0': {'queue_pos': 1}, '1': {'queue_pos': 2}})
    assert tuple(frame[200, 20]) == (0, 220, 0)
    for x in (220, 420):
        assert tuple(frame[200, x]) == (230, 175, 40)
    assert 'Student 0 | HAND RAISED' in labels
    assert 'Student 1 | 2nd Hand Raised' in labels
    assert 'Unassigned | Hand Raised (Unassigned)' in labels
    matches['1'] = (roster[1], detections[1], down)
    frame = engine.render_overlay(image, roster, matches)
    assert tuple(frame[200, 220]) == (230, 175, 40)


def test_frame_pipeline_preserves_names_during_reordering_and_emits_two_raises(monkeypatch):
    from vision_worker import worker as worker_module
    from vision_worker.person_tracker import PersonTracker

    class FakePoseEngine(YOLOPoseEngine):
        def __init__(self, **kwargs):
            self.tracker, self.seat_tracks, self.confidence = PersonTracker(), {}, .5
            self.detections = []

        def infer_frame(self, frame):
            return self.detections

    monkeypatch.setattr(worker_module, 'YOLOPoseEngine', FakePoseEngine)
    monkeypatch.setattr(worker_module, 'FaceEngine', lambda: (_ for _ in ()).throw(FileNotFoundError('No test models')))
    clock = [0.0]
    monkeypatch.setattr(worker_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker_module.time, 'time_ns', lambda: int(clock[0] * 1e9))
    events = []
    worker = VisionWorker(on_event=events.append)
    worker.set_seats([SeatZone(str(i), str(i), name, str(i), i / 2, .1, (i + 1) / 2, 1, student_id=str(i))
                      for i, name in enumerate(('Alice', 'Bob'))])
    worker.set_session_id('session')
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for step in range(25):
        clock[0] = step / 30
        detections = []
        for i, x in enumerate((.25, .75)):
            source = pose()
            shifted = replace(source, **{name: replace(p, x=p.x + x - .5)
                                         for name, p in vars(source).items() if isinstance(p, Point2D)})
            detections.append(PersonDetection((int(x * 640 - 60), 10, int(x * 640 + 60), 420),
                                              (x * 640, 215), shifted, .9))
        worker.pose_engine.detections = detections if step % 2 else detections[::-1]
        worker._process_frame(frame)
    assert [(e.student_name, e.status) for e in events] == [('Alice', 'VALID'), ('Bob', 'VALID')]
    assert all(e.verification_status == 'UNVERIFIED' for e in events)
    worker.restart_camera()
    for step in range(25, 35):
        clock[0] = step / 30
        worker._process_frame(frame)
    assert len([event for event in events if event.status == 'VALID']) == 2
    worker.pose_engine.detections = []
    for now in (1.2, 1.8):
        clock[0] = now
        worker._process_missing_frame()
    assert not worker.podium_queue.get_podium()
