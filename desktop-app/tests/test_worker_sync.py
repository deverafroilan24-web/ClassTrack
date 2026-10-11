"""Camera configuration and roster updates while a session is running."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker import worker as worker_module  # noqa: E402
from launch_desktop import CameraNodeApp  # noqa: E402


def test_hot_swap_accepts_resolved_model_path(monkeypatch):
    class FakeEngine:
        def __init__(self, *args, **kwargs):
            self.seat_tracks = {}

    monkeypatch.setattr(worker_module, "YOLOPoseEngine", FakeEngine)
    worker = object.__new__(worker_module.VisionWorker)
    worker._lock = threading.Lock()
    worker.confidence = 0.5
    worker.model_path = str(Path("models") / "yolo11s-pose.pt")
    worker.pose_engine = FakeEngine()
    worker.pose_engine.seat_tracks = {"desk-1": {"last_x": 0.4}}
    target = str(Path("models") / "yolov8n-pose.pt")
    assert worker.switch_model(target)
    assert worker.model_path == target
    assert worker.pose_engine.seat_tracks == {"desk-1": {"last_x": 0.4}}


def test_reassigned_desk_is_marked_for_tracking_reset():
    worker = object.__new__(worker_module.VisionWorker)
    worker._lock = threading.Lock()
    worker.seats = [SimpleNamespace(id="desk-1", student_id="old", student_name="Old", is_present=True)]
    worker._pending_seat_resets = set()
    worker.set_seats([SimpleNamespace(id="desk-1", student_id="new", student_name="New", is_present=True)])
    assert worker._pending_seat_resets == {"desk-1"}


def test_session_start_keeps_standby_roster_visible_during_sync():
    class FakeWorker:
        def __init__(self):
            self.seats = ["already-loaded-seat"]
            self.session_id = "old-session"

        def set_session_id(self, session_id):
            self.session_id = session_id

        def set_seats(self, seats):
            self.seats = seats

    app = object.__new__(CameraNodeApp)
    app._section_lock = threading.RLock()
    app._active_session_id = "old-session"
    app.section_id = "section-1"
    app.sections = [{"id": "section-1", "name": "Test section"}]
    app.current_section_idx = 0
    app.api_client = SimpleNamespace(session_id="old-session")
    app.vision_worker = FakeWorker()
    app._sync_lock = threading.Lock()
    app._sync_action = None
    app._sync_trigger = threading.Event()

    CameraNodeApp._set_active_session(app, {"id": "new-session", "section_id": "section-1"})

    assert app.vision_worker.session_id is None
    assert app.vision_worker.seats == ["already-loaded-seat"]
    assert app._sync_action == "RELOAD_SEATS"


def test_teacher_section_selection_loads_roster_before_session():
    class FakeWorker:
        def __init__(self):
            self.seats = ["previous-section-seat"]
            self.session_id = None

        def set_session_id(self, session_id):
            self.session_id = session_id

        def set_seats(self, seats):
            self.seats = seats

    app = object.__new__(CameraNodeApp)
    app._section_lock = threading.RLock()
    app._active_session_id = None
    app.section_id = "section-1"
    app.sections = [
        {"id": "section-1", "name": "Section One"},
        {"id": "section-2", "name": "Section Two"},
    ]
    app.current_section_idx = 0
    app.vision_worker = FakeWorker()
    app.api_client = SimpleNamespace(select_section=lambda section_id: None)
    app._sync_lock = threading.Lock()
    app._sync_action = None
    app._sync_trigger = threading.Event()

    CameraNodeApp._set_camera_section(app, "section-2")

    assert app.section_id == "section-2"
    assert app.vision_worker.seats == []
    assert app._sync_action == "RELOAD_SEATS"
    assert app._sync_trigger.is_set()


def test_session_sync_loads_names_skeleton_controls_and_green_first_blue_waiters(monkeypatch):
    import cv2
    import numpy as np
    from unittest.mock import Mock
    from api_client import DashboardAPIClient
    from vision_worker.kinematics import SkeletonPose, Point2D, evaluate_pose_kinematics, KinematicEvaluation
    from vision_worker.podium_queue import PodiumQueueManager
    from vision_worker.state_machine import HandRaiseStateMachine

    app = object.__new__(CameraNodeApp)
    app._section_lock = threading.RLock()
    app._active_session_id = None
    app.section_id = ''
    app.sections = []
    app.current_section_idx = 0
    app._sync_lock = threading.Lock()
    app._sync_action = None
    app._sync_trigger = threading.Event()
    app.api_client = DashboardAPIClient()
    worker = object.__new__(worker_module.VisionWorker)
    worker._lock = threading.Lock()
    worker.seats = []
    worker.session_id = None
    worker._pending_seat_resets = set()
    worker.podium_queue = PodiumQueueManager()
    worker.state_machine = HandRaiseStateMachine(worker.podium_queue, lambda event: None, debounce_duration_sec=.35)
    app.vision_worker = worker
    sections = [{'id': 'class-a', 'name': 'Class A'}, {'id': 'class-b', 'name': 'Class B'}]
    roster = [dict(id=str(i), label=f'Desk {i}', student_name=name, student_id=f'student-{i}',
                   is_present=True, x_min=0, y_min=0, x_max=1, y_max=1)
              for i, name in enumerate(['Alice', 'Bob'])]

    def get(url, **kwargs):
        assert 'X-Edge-Key' not in kwargs['headers']
        if url.endswith('/api/sessions/active'):
            data = {'id': 'session-a', 'section_id': 'class-a'}
        elif url.endswith('/api/sections'):
            data = sections
        elif url.endswith('/api/seats'):
            assert kwargs['params']['section_id'] == 'class-a'
            data = roster
        else:
            assert kwargs['params']['section_id'] == 'class-a'
            data = {'settings': {'show_skeleton': True}}
        return Mock(status_code=200, json=lambda: data)

    monkeypatch.setattr('api_client.requests.get', get)
    ok, session = app.api_client.fetch_active_session_result()
    assert ok
    app._set_active_session(session)
    app._load_sections()
    app._load_seats()
    assert app.section_id == 'class-a'
    assert app.api_client.session_id == worker.session_id == 'session-a'
    assert [s.student_name for s in worker.seats] == ['Alice', 'Bob']
    assert worker.show_skeleton is True

    pose = SkeletonPose(nose=Point2D(.3, .3), left_shoulder=Point2D(.3, .5),
                        left_elbow=Point2D(.3, .3), left_wrist=Point2D(.3, .1))
    valid = evaluate_pose_kinematics(pose)
    for i, timestamp in [(0, 1000), (1, 1100), (0, 1350), (1, 1450)]:
        worker.state_machine.process_seat_frame(str(i), worker.seats[i].student_name, valid,
                                               session_id='session-a', now_ms=timestamp)
    engine = object.__new__(worker_module.YOLOPoseEngine)
    labels = []
    put_text = cv2.putText

    def record_text(image, text, *args, **kwargs):
        labels.append(text)
        return put_text(image, text, *args, **kwargs)

    monkeypatch.setattr(cv2, 'putText', record_text)

    def render(evaluations):
        matches = {str(i): (seat, SimpleNamespace(pose=pose, bbox=(50 + 300*i, 100, 250 + 300*i, 400)), evaluations[i])
                   for i, seat in enumerate(worker.seats)}
        return engine.render_overlay(np.zeros((480, 700, 3), dtype=np.uint8), worker.seats, matches,
                                     podium_entries={e.seat_id: {'queue_pos': e.queue_position} for e in worker.podium_queue.get_podium()})

    frame = render([valid, valid])
    assert tuple(frame[200, 50]) == (0, 220, 0)
    assert tuple(frame[200, 350]) == (230, 175, 40)
    assert 'Alice | HAND RAISED' in labels
    assert 'Bob | 2nd Hand Raised' in labels
    idle = KinematicEvaluation('IDLE', 'NO_RAISE', 0, None, False)
    for timestamp in (1500, 2000):
        worker.state_machine.process_seat_frame('0', 'Alice', idle, session_id='session-a', now_ms=timestamp)
    frame = render([idle, valid])
    assert tuple(frame[200, 50]) == (230, 175, 40)
    assert tuple(frame[200, 350]) == (230, 175, 40)
    assert labels[-2:] == ['Alice | Standby', 'Bob | 2nd Hand Raised']
