"""Production regression cases for the camera-to-ledger path."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_PATH"] = str(Path(tempfile.gettempdir()) / "classtrack-import-test.db")
from backend import app as dashboard  # noqa: E402
from backend.database import DatabaseManager, SCHEMA_SQL  # noqa: E402


@pytest.fixture
def live_class(tmp_path, monkeypatch):
    database = DatabaseManager(str(tmp_path / "classtrack-test.db"))
    monkeypatch.setattr(dashboard, "db", database)
    monkeypatch.setattr(dashboard, "EDGE_API_KEY", "test-edge-key")
    dashboard.edge_selected_section_id = None
    dashboard.queues.clear()
    database.create_teacher("Administrator", "", "ADMIN", "Test-password-2026", is_admin=True, account_id="teacher_master")
    section = database.create_section("Test class", "Testing", "Room 1", teacher_id="teacher_master")
    seat = database.register_student(section["id"], "Student One", "S001")
    token = dashboard._create_teacher_token()
    with TestClient(dashboard.app) as client:
        started = client.post(
            "/api/sessions/start",
            json={"section_id": section["id"], "title": "Test session"},
            headers={"X-Teacher-Token": token},
        )
        assert started.status_code == 201, started.text
        yield client, database, section, seat, token, started.json()
    dashboard.queues.clear()


def event(section, seat, session, event_id, status="VALID", reason="VALID_HAND_RAISE", timestamp=1000):
    return {
        "event_id": event_id, "section_id": section["id"],
        "session_id": session["id"], "seat_id": seat["id"],
        "student_name": seat["student_name"], "student_id": seat["student_id"],
        "status": status, "reason_code": reason, "timestamp_ms": timestamp,
        "arm_angle_deg": 145, "duration_sec": 0.8,
    }


def test_raise_is_saved_once_and_lower_releases_queue(live_class):
    client, database, section, seat, token, session = live_class
    edge_headers = {"X-Edge-Key": "test-edge-key"}
    teacher_headers = {"X-Teacher-Token": token}
    first = event(section, seat, session, "raise-one")
    assert client.post("/api/events/ingest", json=first, headers=edge_headers).status_code == 200
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=teacher_headers).json()
    assert ledger[0]["total_raises"] == 1
    assert ledger[0]["latest_queue_pos"] == 1
    dashboard.queues.clear()
    dashboard._restore_live_queue()
    assert dashboard.session_queue(session["id"]).get_entry(seat["id"]).queue_position == 1

    award = client.post("/api/events/raise-one/award", headers=teacher_headers)
    assert award.status_code == 200, award.text
    duplicate = client.post("/api/events/ingest", json=first, headers=edge_headers)
    assert duplicate.json()["status"] == "duplicate"
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=teacher_headers).json()
    assert ledger[0]["total_raises"] == 1
    assert ledger[0]["total_points"] == 1

    lower = event(section, seat, session, "lower-one", "IDLE", "HAND_LOWERED", 2000)
    assert client.post("/api/events/ingest", json=lower, headers=edge_headers).status_code == 200
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=teacher_headers).json()
    assert ledger[0]["latest_queue_pos"] is None
    assert ledger[0]["total_raises"] == 1


def test_stale_session_and_changed_student_are_rejected(live_class):
    client, database, section, seat, token, session = live_class
    headers = {"X-Edge-Key": "test-edge-key"}
    stale = event(section, seat, session, "stale")
    stale["session_id"] = "old-session"
    assert client.post("/api/events/ingest", json=stale, headers=headers).status_code == 409
    changed = event(section, seat, session, "changed")
    changed["student_name"] = "Former student"
    assert client.post("/api/events/ingest", json=changed, headers=headers).status_code == 409
    assert database.get_events(session_id=session["id"]) == []


def test_live_ledger_rejects_a_different_section(live_class):
    client, database, section, seat, token, session = live_class
    other = database.create_section("Other class", "Testing", "Room 2", teacher_id="teacher_master")
    response = client.get(
        f"/api/recitation/ledger?session_id={session['id']}&section_id={other['id']}",
        headers={"X-Teacher-Token": token},
    )
    assert response.status_code == 400


def test_double_start_does_not_replace_active_session(live_class):
    client, database, section, seat, token, session = live_class
    response = client.post(
        "/api/sessions/start", json={"section_id": section["id"], "title": "Accidental repeat"},
        headers={"X-Teacher-Token": token},
    )
    assert response.status_code == 409
    assert database.get_active_session()["id"] == session["id"]


def test_past_session_never_shows_current_raise_order(live_class):
    client, database, section, seat, token, session = live_class
    headers = {"X-Teacher-Token": token}
    assert client.post("/api/sessions/stop", headers=headers).status_code == 200
    started = client.post(
        "/api/sessions/start", json={"section_id": section["id"], "title": "New session"},
        headers=headers,
    ).json()
    assert client.post(
        "/api/events/ingest", json=event(section, seat, started, "new-session-raise"),
        headers={"X-Edge-Key": "test-edge-key"},
    ).status_code == 200
    old = client.get(
        f"/api/recitation/ledger?session_id={session['id']}&section_id={section['id']}", headers=headers
    ).json()
    assert old[0]["latest_queue_pos"] is None


def test_reassigned_desk_does_not_keep_previous_student_raise(live_class):
    client, database, section, seat, token, session = live_class
    edge_headers = {"X-Edge-Key": "test-edge-key"}
    teacher_headers = {"X-Teacher-Token": token}
    assert client.post(
        "/api/events/ingest", json=event(section, seat, session, "old-raise"), headers=edge_headers
    ).status_code == 200
    replacement = database.enroll_student(section["id"], "Student Two", "S002")
    database.assign_student_to_seat(seat["id"], replacement["id"])
    assert client.get("/api/edge/status").json()["podium_active"] == 0
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=teacher_headers).json()
    assert ledger[0]["student_name"] == "Student Two"
    assert ledger[0]["latest_queue_pos"] is None
    assert client.post(
        "/api/events/ingest", json=event(section, seat, session, "old-retry"), headers=edge_headers
    ).status_code == 409
    new_seat = next(item for item in database.get_seats(section_id=section["id"]) if item["id"] == seat["id"])
    assert client.post(
        "/api/events/ingest", json=event(section, new_seat, session, "new-raise", timestamp=2000),
        headers=edge_headers,
    ).status_code == 200
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=teacher_headers).json()
    assert ledger[0]["latest_queue_pos"] == 1
    assert ledger[0]["total_raises"] == 1
    grades = database.calculate_participation_grades(section_id=section["id"])
    assert next(item for item in grades if item["student_name"] == "Student One")["total_raises"] == 1
    assert next(item for item in grades if item["student_name"] == "Student Two")["total_raises"] == 1


def test_manual_point_button_has_server_action(live_class):
    client, database, section, seat, token, session = live_class
    headers = {"X-Teacher-Token": token}
    awarded = client.post(f"/api/seats/{seat['id']}/award?section_id={section['id']}", headers=headers)
    assert awarded.status_code == 200, awarded.text
    ledger = client.get(f"/api/recitation/ledger?section_id={section['id']}", headers=headers).json()
    assert ledger[0]["total_points"] == 1
    assert ledger[0]["total_raises"] == 0


def test_websocket_uses_same_validation_and_ack(live_class):
    client, database, section, seat, token, session = live_class
    with client.websocket_connect("/ws/edge", headers={"X-Edge-Key": "test-edge-key"}) as socket:
        assert socket.receive_json()["type"] == "EDGE_INIT"
        socket.send_json({"type": "GESTURE_EVENT", "payload": event(section, seat, session, "ws-raise")})
        ack = socket.receive_json()
        assert ack["type"] == "EVENT_ACK"
        assert ack["event_id"] == "ws-raise"
    assert len(database.get_events(session_id=session["id"])) == 1


def test_existing_sqlite_events_gain_student_identity_without_loss(tmp_path):
    path = tmp_path / "legacy.db"
    old_schema = SCHEMA_SQL.replace(
        "    seat_id TEXT,\n    student_id TEXT,\n    student_name TEXT NOT NULL,",
        "    seat_id TEXT,\n    student_name TEXT NOT NULL,",
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO events (id, seat_id, student_name, status, reason_code, arm_angle, duration_sec, timestamp_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-raise", "desk-1", "Original student", "VALID", "VALID_HAND_RAISE", 140, 0.8, 1000),
        )
    database = DatabaseManager(str(path))
    with database._connect() as conn:
        row = conn.execute("SELECT id, student_id FROM events WHERE id = ?", ("legacy-raise",)).fetchone()
    assert row["id"] == "legacy-raise"
    assert row["student_id"] is None


def test_camera_receives_enrolled_face_data(live_class):
    client, database, section, seat, token, session = live_class
    with database._connect() as conn:
        conn.execute("UPDATE students SET face_embedding = ? WHERE id = ?", ("[0.1, 0.2]", seat["student_id"]))
    response = client.get(
        f"/api/seats?section_id={section['id']}", headers={"X-Edge-Key": "test-edge-key"}
    )
    assert response.status_code == 200
    assert response.json()[0]["face_embedding"] == "[0.1, 0.2]"


def test_student_registration_notifies_connected_camera(live_class):
    client, database, section, seat, token, session = live_class
    with client.websocket_connect("/ws/edge", headers={"X-Edge-Key": "test-edge-key"}) as socket:
        assert socket.receive_json()["type"] == "EDGE_INIT"
        registered = client.post(
            f"/api/sections/{section['id']}/students",
            json={"student_name": "New arrival", "student_id_number": "S003"},
            headers={"X-Teacher-Token": token},
        )
        assert registered.status_code == 200
        assert socket.receive_json()["type"] == "SEATS_UPDATED"


def test_camera_can_load_selected_roster_before_session(live_class):
    client, database, section, student, token, session = live_class
    desk = database.configure_seating_grid(section["id"], 1, 1)[0]
    student_id = database.get_students(section["id"])[0]["id"]
    assigned = database.assign_student_to_seat(desk["id"], student_id)
    stopped = client.post("/api/sessions/stop", headers={"X-Teacher-Token": token})
    assert stopped.status_code == 200

    response = client.get(
        f"/api/seats?section_id={section['id']}", headers={"X-Edge-Key": "test-edge-key"}
    )
    assert response.status_code == 200
    assert response.json()[0]["id"] == assigned["id"]
    assert response.json()[0]["student_id"] == student_id


def test_teacher_section_selection_notifies_camera_nodes(live_class, monkeypatch):
    client, database, section, student, token, session = live_class
    messages = []

    async def capture(message):
        messages.append(message)

    monkeypatch.setattr(dashboard.ConnectionManager, "broadcast_to_edge", capture)
    response = client.post(
        "/api/edge/section",
        json={"section_id": section["id"]},
        headers={"X-Teacher-Token": token},
    )
    assert response.status_code == 200
    assert messages == [{"type": "SECTION_SELECTED", "section_id": section["id"]}]
    with client.websocket_connect("/ws/edge", headers={"X-Edge-Key": "test-edge-key"}) as socket:
        init = socket.receive_json()
        assert init["selected_section_id"] == section["id"]
