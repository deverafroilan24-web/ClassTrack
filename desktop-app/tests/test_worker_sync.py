"""Camera configuration and roster updates while a session is running."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("ultralytics")
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
    app._sync_lock = threading.Lock()
    app._sync_action = None
    app._sync_trigger = threading.Event()

    CameraNodeApp._set_camera_section(app, "section-2")

    assert app.section_id == "section-2"
    assert app.vision_worker.seats == []
    assert app._sync_action == "RELOAD_SEATS"
    assert app._sync_trigger.is_set()
