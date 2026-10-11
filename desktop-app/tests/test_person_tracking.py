"""Multi-person simulations run without a webcam or YOLO model download."""

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker.kinematics import Point2D, SkeletonPose
from vision_worker.person_tracker import PersonTracker, assign, appearance_descriptor, appearance_distance
from vision_worker.pose_engine import YOLOPoseEngine, SeatZone, PersonDetection


WIDTH, HEIGHT = 640, 480


def person(x, raised=False, y=.35):
    pose = SkeletonPose(nose=Point2D(x, y - .1),
                        left_shoulder=Point2D(x - .04, y), right_shoulder=Point2D(x + .04, y),
                        left_elbow=Point2D(x - .05, y - .07 if raised else y + .12),
                        left_wrist=Point2D(x - .05, y - .20 if raised else y + .22),
                        right_elbow=Point2D(x + .05, y + .12), right_wrist=Point2D(x + .05, y + .22),
                        left_hip=Point2D(x - .03, y + .25), right_hip=Point2D(x + .03, y + .25),
                        image_aspect_ratio=WIDTH / HEIGHT)
    box = (int((x - .07) * WIDTH), int((y - (.23 if raised else .14)) * HEIGHT),
           int((x + .07) * WIDTH), int((y + .30) * HEIGHT))
    return PersonDetection(box, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), pose, .9)


def seats(count=2):
    return [SeatZone(str(i), f'Desk {i}', f'Student {i}', str(i),
                     i / count, .1, (i + 1) / count, .95, student_id=f'student-{i}')
            for i in range(count)]


def engine():
    result = object.__new__(YOLOPoseEngine)
    result.tracker, result.seat_tracks, result.confidence = PersonTracker(), {}, .5
    return result


def test_assignment_finds_global_optimum_and_allows_unmatched():
    assert set(assign([[.1, .15], [.11, .8]])) == {(0, 1), (1, 0)}
    assert assign([[np.inf], [np.inf]]) == []
    assert assign(np.empty((0, 2))) == []
    assert assign(np.empty((2, 0))) == []


@pytest.mark.parametrize('count', [2, 4, 8])
def test_detector_order_and_hand_dependent_boxes_never_change_names(count):
    tracker, roster = engine(), seats(count)
    initial_ids = {}
    for step in range(100):
        detections = [person((i + .5) / count + .005 * np.sin(step * .4 + i), raised=step % 3 == i % 3)
                      for i in range(count)]
        detections = detections[step % count:] + detections[:step % count]
        if step % 2:
            detections.reverse()
        matches, _ = tracker.match_seats(detections, roster, WIDTH, HEIGHT, now=step / 30)
        if step < 1:
            continue
        for i in range(count):
            det = matches[str(i)][1]
            assert det is not None
            assert abs(det.pose.nose.x - (i + .5) / count) < .01
            initial_ids.setdefault(str(i), det.track_id)
            assert det.track_id == initial_ids[str(i)]


def test_two_people_cross_seat_boundaries_with_same_clothing_and_full_overlap():
    tracker, roster = engine(), seats()
    original = {}
    observed = 0
    for step in range(91):
        fraction = max(0, step - 5) / 85
        positions = [.2 + .6 * fraction, .8 - .6 * fraction]
        detections = [person(x, raised=step % 5 == 0) for x in positions]
        if step % 2:
            detections.reverse()
        matches, _ = tracker.match_seats(detections, roster, WIDTH, HEIGHT, now=step / 30)
        for i in range(2):
            det = matches[str(i)][1]
            if det is None:
                continue  # Ambiguous overlap must withhold names, not guess.
            original.setdefault(str(i), det.track_id)
            assert det.track_id == original[str(i)]
            assert det.pose.nose.x == pytest.approx(positions[i])
            observed += 1
    assert observed > 140
    assert matches['0'][1].pose.nose.x > .7
    assert matches['1'][1].pose.nose.x < .3


@pytest.mark.parametrize('count', [4, 8])
def test_multiple_pairs_cross_simultaneously_and_keep_their_names(count):
    tracker, roster = engine(), seats(count)
    original = {}
    for step in range(91):
        fraction = max(0, step - 5) / 85
        positions = []
        for i in range(count):
            start = (i + .5) / count
            end = (i + (1 if i % 2 == 0 else -1) + .5) / count
            positions.append(start + (end - start) * fraction)
        detections = [person(x) for x in positions]
        if step % 2:
            detections.reverse()
        matches, _ = tracker.match_seats(detections, roster, WIDTH, HEIGHT, now=step / 30)
        for i, x in enumerate(positions):
            det = matches[str(i)][1]
            if det is not None:
                original.setdefault(str(i), det.track_id)
                assert det.track_id == original[str(i)]
                assert det.pose.nose.x == pytest.approx(x)
    assert all(det is not None for _, det, _ in matches.values())


def test_temporary_disappearance_reserves_name_and_returns_to_same_track():
    tracker, roster = engine(), seats()
    for step in range(4):
        matches, _ = tracker.match_seats([person(.25), person(.75)], roster, WIDTH, HEIGHT, now=step / 30)
    original = matches['0'][1].track_id
    for step in range(4, 44):
        # The visible student moves into the absent student's seat.
        matches, _ = tracker.match_seats([person(.75 - (step - 4) * .012)], roster, WIDTH, HEIGHT, now=step / 30)
        assert matches['0'][1] is None
        assert matches['1'][1] is not None
    matches, _ = tracker.match_seats([person(.25), person(.75 - 40 * .012)], roster, WIDTH, HEIGHT, now=44 / 30)
    # At almost identical positions, a safe suspension is acceptable.
    matches, _ = tracker.match_seats([person(.25), person(.35)], roster, WIDTH, HEIGHT, now=45 / 30)
    if matches['0'][1] is not None:
        assert matches['0'][1].track_id == original
        assert matches['0'][1].pose.nose.x == .25


def test_short_occlusion_reacquires_without_order_dependence():
    tracker, roster = engine(), seats()
    for step in range(4):
        matches, _ = tracker.match_seats([person(.25), person(.75)], roster, WIDTH, HEIGHT, now=step / 30)
    original = matches['0'][1].track_id
    for step in range(4, 20):
        matches, _ = tracker.match_seats([person(.75)], roster, WIDTH, HEIGHT, now=step / 30)
    matches, _ = tracker.match_seats([person(.75), person(.26)], roster, WIDTH, HEIGHT, now=20 / 30)
    assert matches['0'][1].track_id == original
    assert matches['0'][1].pose.nose.x == .26


def test_motion_prediction_reacquires_a_moving_person_after_occlusion():
    tracker, roster = engine(), seats()
    original = None
    for step in range(40):
        moving = person(.2 + .012 * step)
        detections = [person(.9)] if 12 <= step <= 20 else [person(.9), moving]
        matches, _ = tracker.match_seats(detections, roster, WIDTH, HEIGHT, now=step / 30)
        if step == 1:
            original = matches['0'][1].track_id
        if step >= 21:
            assert matches['0'][1].track_id == original
            assert matches['0'][1].pose.nose.x == pytest.approx(.2 + .012 * step)


def test_losing_one_shoulder_does_not_move_anchor_up_to_nose():
    tracker, roster = engine(), seats()
    for step in range(4):
        matches, _ = tracker.match_seats([person(.25)], roster, WIDTH, HEIGHT, now=step / 30)
    original = matches['0'][1].track_id
    partial = person(.25)
    partial.pose = replace(partial.pose, right_shoulder=None)
    matches, _ = tracker.match_seats([partial], roster, WIDTH, HEIGHT, now=.15)
    assert matches['0'][1].track_id == original


def test_unrelated_person_cannot_take_reserved_seat_name():
    tracker, roster = engine(), seats()
    for step in range(3):
        matches, _ = tracker.match_seats([person(.2), person(.8)], roster, WIDTH, HEIGHT, now=step / 30)
    matches, unassigned = tracker.match_seats([person(.8), person(.48)], roster, WIDTH, HEIGHT, now=.2)
    assert matches['0'][1] is None
    assert matches['1'][1].pose.nose.x == .8
    assert any(det.pose.nose.x == .48 for det in unassigned)


def test_outside_seat_is_never_assigned_by_detection_list_position():
    tracker = engine()
    roster = [replace(seats()[0], x_max=.3)]
    for step in range(3):
        matches, unassigned = tracker.match_seats([person(.8)], roster, WIDTH, HEIGHT, now=step / 30)
        assert matches['0'][1] is None
        assert len(unassigned) == 1


def test_identical_seat_zones_do_not_guess_initial_identity():
    tracker = engine()
    roster = [replace(seat, x_min=0, x_max=1) for seat in seats()]
    for step in range(4):
        matches, unassigned = tracker.match_seats([person(.3), person(.7)], roster, WIDTH, HEIGHT, now=step / 30)
    assert all(det is None for _, det, _ in matches.values())
    assert len(unassigned) == 2


def test_lost_tracks_expire_and_ids_are_never_reused_after_reset():
    tracker = engine()
    roster = seats()
    for step in range(3):
        matches, _ = tracker.match_seats([person(.25)], roster, WIDTH, HEIGHT, now=step / 30)
    old_id = matches['0'][1].track_id
    tracker.match_seats([], roster, WIDTH, HEIGHT, now=4)
    assert not tracker.tracker.tracks
    for step in range(2):
        matches, _ = tracker.match_seats([person(.25)], roster, WIDTH, HEIGHT, now=5 + step / 30)
    assert matches['0'][1].track_id > old_id
    tracker.reset_tracking()
    assert not tracker.seat_tracks
    assert not tracker.tracker.tracks


def test_real_colour_descriptor_distinguishes_shirts():
    det = person(.25)
    red = np.full((HEIGHT, WIDTH, 3), (0, 0, 230), dtype=np.uint8)
    blue = np.full((HEIGHT, WIDTH, 3), (230, 0, 0), dtype=np.uint8)
    a = appearance_descriptor(red, det, WIDTH, HEIGHT)
    b = appearance_descriptor(blue, det, WIDTH, HEIGHT)
    assert appearance_distance(a, a) == pytest.approx(0)
    assert appearance_distance(a, b) > .7


def test_small_lighting_change_does_not_reject_same_shirt_at_histogram_boundary():
    det = person(.25)
    dark = np.full((HEIGHT, WIDTH, 3), (63, 0, 0), dtype=np.uint8)
    brighter = np.full((HEIGHT, WIDTH, 3), (65, 0, 0), dtype=np.uint8)
    a = appearance_descriptor(dark, det, WIDTH, HEIGHT)
    b = appearance_descriptor(brighter, det, WIDTH, HEIGHT)
    assert appearance_distance(a, b) < .7
