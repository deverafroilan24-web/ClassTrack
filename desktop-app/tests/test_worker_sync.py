"""Camera configuration and roster updates while a session is running."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("ultralytics")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_worker import worker as worker_module  # noqa: E402


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
