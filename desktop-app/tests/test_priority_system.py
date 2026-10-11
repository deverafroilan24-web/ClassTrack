"""Priority comes from observed onset, never frame/detection iteration order."""

import sys
from itertools import permutations
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker import worker as worker_module
from vision_worker.display_state import BLUE, GREEN, ordinal, person_display_state
from vision_worker.gesture_filter import GestureFilter
from vision_worker.kinematics import KinematicEvaluation
from vision_worker.person_tracker import PersonTracker
from vision_worker.pose_engine import YOLOPoseEngine
from vision_worker.podium_queue import PodiumQueueManager
from vision_worker.state_machine import HandRaiseStateMachine
from test_person_tracking import person, seats


VALID = KinematicEvaluation('VALID', 'VALID_HAND_RAISE', 170, 'left', True)


@pytest.mark.parametrize('order', list(permutations(['c', 'a', 'b'])))
def test_timestamp_ties_have_same_result_for_every_insertion_order(order):
    queue = PodiumQueueManager()
    for seat_id in order:
        queue.register_raise(seat_id, seat_id, 170, timestamp_ms=1000)
    assert [entry.seat_id for entry in queue.get_podium()] == ['a', 'b', 'c']
    assert [entry.delta_ms for entry in queue.get_podium()] == [0, 0, 0]


def test_delayed_confirmation_keeps_original_observation_priority():
    events, queue = [], PodiumQueueManager()
    machine = HandRaiseStateMachine(queue, events.append)
    machine.process_seat_frame('z', 'First', VALID, now_ms=1000, clock_ms=0, allow_confirmation=False)
    machine.process_seat_frame('a', 'Second', VALID, now_ms=1040, clock_ms=40)
    machine.process_seat_frame('a', 'Second', VALID, now_ms=1250, clock_ms=250)
    machine.process_seat_frame('z', 'First', VALID, now_ms=1400, clock_ms=400, allow_confirmation=True)
    assert [event.student_name for event in events] == ['Second', 'First']
    assert [entry.student_name for entry in queue.get_podium()] == ['First', 'Second']
    assert queue.get_entry('z').timestamp_ms == 1000
    assert queue.get_entry('a').delta_ms == 40


def test_hysteresis_keeps_original_raise_onset_and_resets_after_lower():
    signal = GestureFilter()
    signal.update(1, VALID, 0)
    signal.update(1, VALID, .04)
    unknown = KinematicEvaluation('IDLE', 'KEYPOINTS_MISSING', 0, None, False)
    signal.update(1, unknown, .08)
    signal.update(1, VALID, .12)
    assert signal.raise_started_at(1) == 0
    lowered = KinematicEvaluation('IDLE', 'NO_RAISE', 0, None, False)
    signal.update(1, lowered, .20)
    signal.update(1, lowered, .30)
    assert signal.raise_started_at(1) is None
    signal.update(1, VALID, .40)
    signal.update(1, VALID, .45)
    assert signal.raise_started_at(1) == .40


@pytest.mark.parametrize('rank,assigned,expected', [(1, True, 'HAND RAISED'), (2, True, '2nd Hand Raised'),
                                                  (5, True, '5th Hand Raised'), (None, True, 'Hand Raised (Unassigned)'),
                                                  (1, False, 'Hand Raised (Unassigned)')])
def test_label_and_indicator_share_one_person_display_state(rank, assigned, expected):
    display = person_display_state('JM', VALID, rank, assigned=assigned)
    assert display.status == expected
    assert display.label == f'JM | {expected}'
    assert display.box_color == (GREEN if expected == 'HAND RAISED' else BLUE)
    assert display.pill_color == ((0, 150, 0) if expected == 'HAND RAISED' else (140, 95, 20))


class SimulatedEngine(YOLOPoseEngine):
    def __init__(self, **kwargs):
        self.tracker, self.seat_tracks, self.confidence = PersonTracker(), {}, .5
        self.detections = []

    def infer_frame(self, frame):
        return self.detections


def simulated_worker(monkeypatch, count):
    monkeypatch.setattr(worker_module, 'YOLOPoseEngine', SimulatedEngine)
    monkeypatch.setattr(worker_module, 'FaceEngine', lambda: (_ for _ in ()).throw(FileNotFoundError('Simulation')))
    clock, epoch, events = [0.0], [100_000], []
    monkeypatch.setattr(worker_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker_module.time, 'time_ns', lambda: int(epoch[0]*1e6))
    worker = worker_module.VisionWorker(on_event=events.append)
    worker.set_seats(seats(count))
    worker.set_session_id('priority-simulation')
    labels = []
    original_put_text = cv2.putText

    def record(image, text, *args, **kwargs):
        labels.append(text)
        return original_put_text(image, text, *args, **kwargs)

    monkeypatch.setattr(cv2, 'putText', record)
    return worker, clock, epoch, events, labels


def frame_step(worker, clock, labels, step, count, raised):
    clock[0] = step / 30
    detections = [person((i+.5)/count, raised=i in raised) for i in range(count)]
    worker.pose_engine.detections = detections[step % count:] + detections[:step % count]
    if step % 2:
        worker.pose_engine.detections.reverse()
    labels.clear()
    return worker._process_frame(np.zeros((480, 640, 3), dtype=np.uint8))


def assert_priority_colors(frame, count, first):
    for i in range(count):
        x = int(((i+.5)/count-.07)*640)
        assert tuple(frame[210, max(0, x)]) == (GREEN if i == first else BLUE)


def test_five_simultaneous_raises_keep_original_ordinals_after_first_lowers(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 5)
    worker.set_seats(list(reversed(worker.seats)))
    for step in range(20):
        frame = frame_step(worker, clock, labels, step, 5, set(range(5)))
        if step >= 2:
            assert labels.count('Student 0 | HAND RAISED') == 1
            assert all(f'Student {i} | {ordinal(i+1)} Hand Raised' in labels for i in range(1, 5))
            assert_priority_colors(frame, 5, first=0)
    assert len([event for event in events if event.status == 'VALID']) == 5
    assert [event.seat_id for event in events if event.status == 'VALID'] == list(map(str, range(5)))
    assert len({event.timestamp_ms for event in events}) == 1
    for step in range(20, 30):
        frame = frame_step(worker, clock, labels, step, 5, {1, 2, 3, 4})
    assert 'Student 1 | 2nd Hand Raised' in labels
    assert 'Student 0 | Standby' in labels
    assert all(f'Student {i} | {ordinal(i+1)} Hand Raised' in labels for i in range(2, 5))
    assert len([event for event in events if event.status == 'VALID']) == 5
    assert_priority_colors(frame, 5, first=None)
    assert [entry.queue_position for entry in worker.podium_queue.get_podium()] == [2, 3, 4, 5]


def test_earliest_observed_person_wins_despite_position_and_clock_jump(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 3)
    for step in range(5):
        frame_step(worker, clock, labels, step, 3, set())
    for step in range(5, 25):
        if step == 8:
            epoch[0] = 1000  # Wall clock moves backwards; camera order stays monotonic.
        frame_step(worker, clock, labels, step, 3, {2} if step < 8 else {0, 1, 2})
        if step >= 10:
            assert 'Student 2 | HAND RAISED' in labels
            assert 'Student 0 | 2nd Hand Raised' in labels
            assert 'Student 1 | 3rd Hand Raised' in labels
    assert [entry.student_name for entry in worker.podium_queue.get_podium()] == ['Student 2', 'Student 0', 'Student 1']
    assert [entry.delta_ms for entry in worker.podium_queue.get_podium()] == [0, 100, 100]


def test_missing_leader_never_promotes_second_and_reappearance_keeps_original_order(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    for step in range(20):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    for step in range(20, 40):
        clock[0] = step / 30
        worker.pose_engine.detections = [person(.75, True)]
        labels.clear()
        worker._process_frame(np.zeros((480, 640, 3), dtype=np.uint8))
        if step == 21:
            assert labels == ['Student 1 | 2nd Hand Raised']
    assert labels == ['Student 1 | 2nd Hand Raised']
    assert len([event for event in events if event.status == 'VALID']) == 2
    # Reappearing after a sustained absence gets a new onset, not its old timestamp.
    for step in range(40, 110):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    assert labels == ['Student 0 | HAND RAISED', 'Student 1 | 2nd Hand Raised']
    valid_events = [event for event in events if event.status == 'VALID']
    assert len(valid_events) == 3
    assert valid_events[-1].timestamp_ms > valid_events[0].timestamp_ms


def test_slow_first_verification_keeps_camera_and_confirmed_queue_in_same_order(monkeypatch):
    from types import SimpleNamespace
    from vision_worker.face_verifier import FaceVerifier

    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 3)

    class ControlledVerifier:
        key = staticmethod(FaceVerifier.key)

        def clear(self):
            pass

        def prune(self, keys):
            pass

        def request(self, seat, det, frame, now):
            pass

        def get(self, seat, det, now):
            if seat.id == '2' and now < .7:
                return None
            return SimpleNamespace(match=True, similarity=.9)

    worker.face_verifier = ControlledVerifier()
    for seat in worker.seats:
        seat.face_embedding = '[1.0, 0.0]'
    worker.set_seats(list(reversed(worker.seats)))
    for step in range(5):
        frame_step(worker, clock, labels, step, 3, set())
    for step in range(5, 30):
        frame_step(worker, clock, labels, step, 3, {2} if step < 8 else {0, 1, 2})
        if 10 <= step < 21:
            assert labels.count('Student 2 | HAND RAISED') == 1
            assert len([event for event in events if event.status == 'VALID']) == 0
    assert [event.student_name for event in events if event.status == 'VALID'] == ['Student 2', 'Student 0', 'Student 1']
    assert [entry.student_name for entry in worker.podium_queue.get_podium()] == ['Student 2', 'Student 0', 'Student 1']
    assert all(event.verification_status == 'VERIFIED' for event in events if event.status == 'VALID')


def test_priority_labels_follow_people_through_crossing_without_extra_events(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    seen_after_crossing = False
    for step in range(100):
        clock[0] = step / 30
        fraction = min(1, max(0, step - 12) / 85)
        positions = [.2 + .6*fraction, .8 - .6*fraction]
        detections = [person(x, raised=step >= (5 if i == 0 else 8)) for i, x in enumerate(positions)]
        worker.pose_engine.detections = detections if step % 2 else detections[::-1]
        labels.clear()
        output = worker._process_frame(np.zeros((480, 640, 3), dtype=np.uint8))
        if step > 20:
            for label in labels:
                if label.startswith('Student 0 |'):
                    assert label == 'Student 0 | HAND RAISED'
                if label.startswith('Student 1 |'):
                    assert label == 'Student 1 | 2nd Hand Raised'
        if step > 92:
            assert 'Student 0 | HAND RAISED' in labels
            assert 'Student 1 | 2nd Hand Raised' in labels
            seen_after_crossing = True
    assert seen_after_crossing
    assert len([event for event in events if event.status == 'VALID']) == 2


@pytest.mark.parametrize('reason,status,color', [('ERR_DOUBLE_HAND_RAISE', 'DOUBLE_HAND', (0, 140, 255)),
                                               ('SEAT_MISMATCH', 'MISMATCH', (0, 0, 240))])
def test_existing_warning_states_keep_distinct_labels_and_colors(reason, status, color):
    invalid = KinematicEvaluation('INVALID', reason, 0, None, False)
    display = person_display_state('JM', invalid, 1)
    assert display.status == status
    assert display.box_color == color
    assert 'HAND RAISED' not in display.label


def test_new_session_during_camera_loss_does_not_inherit_previous_raise_timestamp(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    for step in range(20):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    old_timestamp = events[0].timestamp_ms
    worker.set_session_id('new-session')
    clock[0] = 1.0
    worker._process_missing_frame()
    for step in range(31, 45):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    new_events = [event for event in events if event.session_id == 'new-session' and event.status == 'VALID']
    assert len(new_events) == 2
    assert all(event.timestamp_ms > old_timestamp for event in new_events)


def test_changed_student_cannot_inherit_previous_students_priority_timestamp(monkeypatch):
    from dataclasses import replace

    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    for step in range(20):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    old_timestamp = worker.podium_queue.get_entry('0').timestamp_ms
    roster = list(worker.seats)
    roster[0] = replace(roster[0], student_id='replacement', student_name='Replacement')
    worker.set_seats(roster)
    for step in range(20, 35):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    assert 'Student 1 | 2nd Hand Raised' in labels
    assert 'Replacement | 3rd Hand Raised' in labels
    assert worker.podium_queue.get_entry('0').timestamp_ms > old_timestamp


def test_brief_missing_candidate_reserves_priority_before_confirmation(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    for step in range(5):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    assert not events
    for step in (5, 6):
        clock[0] = step / 30
        worker.pose_engine.detections = [person(.75, True)]
        labels.clear()
        worker._process_frame(np.zeros((480, 640, 3), dtype=np.uint8))
        assert labels == ['Student 1 | 2nd Hand Raised']
    frame_step(worker, clock, labels, 7, 2, {0, 1})
    assert 'Student 0 | HAND RAISED' in labels
    assert 'Student 1 | 2nd Hand Raised' in labels
    assert [event.seat_id for event in events if event.status == 'VALID'] == ['0', '1']


@pytest.mark.parametrize('rank,expected', [(2, '2nd'), (3, '3rd'), (4, '4th'), (11, '11th'),
                                         (12, '12th'), (13, '13th'), (21, '21st'), (22, '22nd'), (23, '23rd')])
def test_ordinal_labels(rank, expected):
    assert ordinal(rank) == expected


def test_all_hands_observed_lower_resets_next_question_and_new_first_can_be_different(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 3)
    for step in range(20):
        frame_step(worker, clock, labels, step, 3, {0, 1, 2})
    for step in range(20, 40):
        frame_step(worker, clock, labels, step, 3, set())
    assert not worker.question_order.timestamps
    assert not worker.podium_queue.get_podium()
    for step in range(40, 52):
        frame_step(worker, clock, labels, step, 3, {2})
    assert 'Student 2 | HAND RAISED' in labels
    for step in range(52, 64):
        frame = frame_step(worker, clock, labels, step, 3, {0, 1, 2})
    assert 'Student 2 | HAND RAISED' in labels
    assert 'Student 0 | 2nd Hand Raised' in labels
    assert 'Student 1 | 3rd Hand Raised' in labels
    assert_priority_colors(frame, 3, first=2)
    assert len([event for event in events if event.status == 'VALID']) == 6


def test_short_all_down_flicker_and_camera_loss_do_not_reset_question(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 2)
    for step in range(20):
        frame_step(worker, clock, labels, step, 2, {0, 1})
    timestamps = dict(worker.question_order.timestamps)
    for step in range(20, 24):
        frame_step(worker, clock, labels, step, 2, set())
    assert worker.question_order.timestamps == timestamps
    clock[0] = 2.0
    worker._process_missing_frame()
    clock[0] = 3.0
    worker._process_missing_frame()
    assert worker.question_order.timestamps == timestamps
    for step in range(92, 100):
        frame_step(worker, clock, labels, step, 2, {1})
    assert 'Student 1 | 2nd Hand Raised' in labels


def test_later_student_keeps_fourth_place_after_original_first_lowers(monkeypatch):
    worker, clock, epoch, events, labels = simulated_worker(monkeypatch, 4)
    for step in range(20):
        frame_step(worker, clock, labels, step, 4, {0, 1, 2})
    for step in range(20, 35):
        frame = frame_step(worker, clock, labels, step, 4, {1, 2, 3})
    assert 'Student 0 | Standby' in labels
    assert 'Student 1 | 2nd Hand Raised' in labels
    assert 'Student 2 | 3rd Hand Raised' in labels
    assert 'Student 3 | 4th Hand Raised' in labels
    assert_priority_colors(frame, 4, first=None)
    assert [entry.queue_position for entry in worker.podium_queue.get_podium()] == [2, 3, 4]
    assert [event.seat_id for event in events if event.status == 'VALID'] == ['0', '1', '2', '3']
