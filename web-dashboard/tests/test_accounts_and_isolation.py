"""Credential security, input validation and simultaneous classroom regressions."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_live_session import dashboard, DatabaseManager, event

PASSWORD = 'Teacher-test-password-2026'


@pytest.fixture
def school(tmp_path, monkeypatch):
    database = DatabaseManager(str(tmp_path / 'school.db'))
    monkeypatch.setattr(dashboard, 'db', database)
    monkeypatch.setattr(dashboard, 'EDGE_API_KEY', 'edge-test')
    dashboard.queues.clear()
    dashboard.login_attempts.clear()
    dashboard.teacher_camera_settings.clear()
    admin = database.create_teacher('Administrator', '', 'ADMIN', PASSWORD, is_admin=True, account_id='teacher_master')
    first = database.create_teacher('Teacher One', 'Science', 'T-001', PASSWORD)
    second = database.create_teacher('Teacher Two', 'Math', 'T-002', PASSWORD)
    with TestClient(dashboard.app) as client:
        def headers(account):
            response = client.post('/api/auth/login', json={'login_id': account['login_id'], 'password': PASSWORD})
            assert response.status_code == 200, response.text
            return {'X-Teacher-Token': response.json()['token']}
        yield client, database, headers(admin), headers(first), headers(second)


def section(client, headers, name='Science'):
    response = client.post('/api/sections', headers=headers, json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()


def test_admin_crud_password_reset_and_revocation(school):
    client, db, admin, one, two = school
    payload = {'name': 'New Teacher', 'department': '', 'login_id': 'T-003', 'password': PASSWORD}
    assert client.post('/api/admin/teachers', headers=one, json=payload).status_code == 403
    created = client.post('/api/admin/teachers', headers=admin, json=payload)
    assert created.status_code == 201, created.text
    teacher_id = created.json()['id']
    assert client.post('/api/admin/teachers', headers=admin, json={**payload, 'login_id': 't-003'}).status_code == 409
    login = client.post('/api/auth/login', json={'login_id': 't-003', 'password': PASSWORD}).json()
    old = {'X-Teacher-Token': login['token']}
    assert not login['is_admin']
    updated = client.put('/api/admin/teachers/' + teacher_id, headers=admin, json={**payload, 'password': 'Replacement-password-2026'})
    assert updated.status_code == 200, updated.text
    assert client.get('/api/sections', headers=old).status_code == 401
    assert client.post('/api/auth/login', json={'login_id': 'T-003', 'password': PASSWORD}).status_code == 401
    assert client.post('/api/auth/login', json={'login_id': 'T-003', 'password': 'Replacement-password-2026'}).status_code == 200
    # Editing the profile without a password preserves the current password.
    payload.pop('password')
    assert client.put('/api/admin/teachers/' + teacher_id, headers=admin, json=payload).status_code == 200
    assert client.delete('/api/admin/teachers/' + teacher_id, headers=admin).status_code == 200
    assert client.post('/api/auth/login', json={'login_id': 'T-003', 'password': 'Replacement-password-2026'}).status_code == 401
    assert db.get_teacher(teacher_id)['is_active'] == 0
    assert client.delete('/api/admin/teachers/teacher_master', headers=admin).status_code == 404


def test_password_storage_and_legacy_routes(school):
    client, db, admin, one, two = school
    with db._connect() as conn:
        rows = conn.execute('SELECT pin, pin_hash, password_hash FROM teachers').fetchall()
        admins = conn.execute('SELECT password_hash FROM "Admin"').fetchall()
    assert all(r['pin'] is None and r['pin_hash'] is None for r in rows)
    assert all(r['password_hash'].startswith('pbkdf2_sha256$600000$') for r in rows)
    assert len({r['password_hash'] for r in [*rows, *admins]}) == 3  # independent salts
    assert PASSWORD not in client.get('/api/admin/teachers', headers=admin).text
    assert client.get('/api/auth/teacher-keys').status_code in (401, 404)
    assert client.post('/api/auth/verify-pin', json={'pin': '1234'}).status_code in (401, 404)
    assert client.get('/api/admin/teachers').status_code == 401


def test_admin_table_email_reset_and_validation(school):
    client, db, admin, one, two = school
    account = db.save_admin('Admin1', 'New-admin-password-2026', 'admin1@example.com')
    assert account['email'] == 'admin1@example.com'
    assert 'password_hash' not in account
    assert client.get('/api/admin/teachers', headers=admin).status_code == 401
    login = client.post('/api/auth/login', json={'login_id': 'admin1', 'password': 'New-admin-password-2026'})
    assert login.status_code == 200
    assert login.json()['is_admin'] is True
    assert client.get('/api/admin/teachers', headers={'X-Teacher-Token': login.json()['token']}).status_code == 200
    with db._connect() as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM teachers WHERE is_admin = 1').fetchone()['n'] == 0
        stored = conn.execute('SELECT password_hash FROM "Admin"').fetchone()['password_hash']
        assert stored.startswith('pbkdf2_sha256$600000$')
        assert 'New-admin-password-2026' not in stored
    with pytest.raises(ValueError):
        db.save_admin('Admin1', PASSWORD, 'invalid-email')
    with pytest.raises(ValueError):
        db.save_admin('Admin1', 'pass', 'admin1@example.com')
    with pytest.raises(ValueError):
        db.create_teacher('Collision', '', 'ADMIN1', PASSWORD)
    with pytest.raises(ValueError):
        db.save_admin('T-001', PASSWORD, 'admin1@example.com')


def test_admin_table_migrates_legacy_credentials_once(tmp_path):
    from backend.database import hash_password
    path = str(tmp_path / 'legacy-admin.db')
    db = DatabaseManager(path)
    password_hash = hash_password(PASSWORD)
    with db._connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'admin_table_v1'")
        conn.execute('''INSERT INTO teachers
            (id, name, login_id, password_hash, is_admin, is_active, auth_version, created_at)
            VALUES ('teacher_master', 'Administrator', 'ADMIN1', ?, 1, 1, 4, '2026-01-01')''', (password_hash,))
    section = db.create_section('Keep class', '', '', 'teacher_master')
    migrated = DatabaseManager(path)
    assert migrated.authenticate_account('Admin1', PASSWORD)['is_admin'] == 1
    assert migrated.authenticate_teacher('Admin1', PASSWORD) is None
    assert migrated.get_admin('teacher_master')['auth_version'] == 5
    assert migrated.get_section_owner(section['id']) == 'teacher_master'
    with migrated._connect() as conn:
        assert conn.execute('SELECT password_hash FROM "Admin"').fetchone()['password_hash'] == password_hash
        assert conn.execute('SELECT password_hash FROM teachers').fetchone()['password_hash'] is None
    again = DatabaseManager(path)
    assert again.get_admin('teacher_master')['auth_version'] == 5


def test_rate_limit_and_invalid_credentials(school):
    client, db, admin, one, two = school
    for _ in range(5):
        assert client.post('/api/auth/login', json={'login_id': 'T-001', 'password': 'wrong'}).status_code == 401
    assert client.post('/api/auth/login', json={'login_id': 'T-001', 'password': PASSWORD}).status_code == 429
    assert client.post('/api/auth/login', json={'login_id': 'T-002', 'password': PASSWORD}).status_code == 200


def test_duplicate_enrollment_and_scope(school):
    client, db, admin, one, two = school
    a, b = section(client, one), section(client, two)
    path = f"/api/sections/{a['id']}/students/enroll"
    student = {'name': '  Maria  Cruz ', 'student_id_number': 's-001'}
    assert client.post(path, headers=one, json=student).status_code == 200
    assert client.post(path, headers=one, json={**student, 'student_id_number': ' S-001 '}).status_code == 409
    assert client.post(path, headers=one, json={**student, 'name': 'Different name'}).status_code == 409
    assert client.post(path, headers=one, json={**student, 'student_id_number': 'S-002'}).status_code == 200
    assert client.post(f"/api/sections/{b['id']}/students/enroll", headers=two, json=student).status_code == 200
    assert client.post(f"/api/sections/{a['id']}/students", headers=one, json={'student_name': 'Maria Cruz', 'student_id_number': 'S-001'}).status_code == 409
    assert len(db.get_students(a['id'])) == 2
    assert client.get(path.removesuffix('/enroll'), headers=two).status_code == 404


def test_concurrent_duplicate_submissions_are_atomic(school):
    client, db, admin, one, two = school
    sec = section(client, one)
    def enroll(_):
        try:
            db.enroll_student(sec['id'], 'Same Student', 'S-1')
            return 'created'
        except ValueError:
            return 'duplicate'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(enroll, range(4)))
    assert results.count('created') == 1
    assert len(db.get_students(sec['id'])) == 1


@pytest.mark.parametrize('payload', [
    {'name': '   ', 'student_id_number': 'S-1'},
    {'name': '<script>alert(1)</script>', 'student_id_number': 'S-1'},
    {'name': 'Student', 'student_id_number': ''},
    {'name': 'Student', 'student_id_number': 'invalid id'},
    {'name': 'x' * 121, 'student_id_number': 'S-1'},
    {'name': 'Student', 'student_id_number': 'S-1', 'is_admin': True},
    {'name': 'Student', 'student_id_number': 'S-1', 'photo_base64': 'not an image'},
])
def test_enrollment_validation(school, payload):
    client, db, admin, one, two = school
    sec = section(client, one)
    assert client.post(f"/api/sections/{sec['id']}/students/enroll", headers=one, json=payload).status_code == 422
    assert db.get_students(sec['id']) == []


def test_two_live_classes_queue_guest_and_camera_isolation(school):
    client, db, admin, one, two = school
    a, b = section(client, one, 'Class A'), section(client, two, 'Class B')
    seat_a = db.register_student(a['id'], 'Student A', 'A-1')
    seat_b = db.register_student(b['id'], 'Student B', 'B-1')
    edge = {'X-Edge-Key': 'edge-test'}
    with client.websocket_connect('/ws/edge?section_id=' + a['id'], headers=edge) as camera:
        camera.receive_json()
        sa = client.post('/api/sessions/start', headers=one, json={'section_id': a['id']}).json()
        assert camera.receive_json()['session_id'] == sa['id']
        sb_response = client.post('/api/sessions/start', headers=two, json={'section_id': b['id']})
        assert sb_response.status_code == 201, sb_response.text
        sb = sb_response.json()
        camera.send_text('ping')
        assert camera.receive_text() == 'pong'  # B's start was not sent to A's camera.
        for sec, seat, session, event_id in [(a, seat_a, sa, 'a-event'), (b, seat_b, sb, 'b-event')]:
            assert client.post('/api/events/ingest', headers=edge, json=event(sec, seat, session, event_id)).status_code == 200
        guest_headers = []
        for headers in (one, two):
            code = client.get('/api/sessions/active/guest-access', headers=headers).json()['code']
            guest = client.post('/api/auth/guest', json={'code': code})
            assert guest.status_code == 200, guest.text
            guest_headers.append({'X-Guest-Token': guest.json()['token']})
        for headers, name in [(one, 'Student A'), (two, 'Student B'), (guest_headers[0], 'Student A'), (guest_headers[1], 'Student B')]:
            ledger = client.get('/api/recitation/ledger', headers=headers).json()
            assert len(ledger) == 1 and ledger[0]['student_name'] == name
            assert ledger[0]['latest_queue_pos'] == 1
        assert client.get('/api/sessions/' + sb['id'], headers=one).status_code == 404
        assert client.post('/api/events/b-event/award', headers=one).status_code == 404
        client.post('/api/sessions/stop', headers=one)
        assert camera.receive_json()['type'] == 'SESSION_STOPPED'
        assert client.get('/api/sessions/active', headers=two).json()['id'] == sb['id']
        assert client.get('/api/recitation/ledger', headers=two).json()[0]['latest_queue_pos'] == 1
        assert client.get('/api/auth/guest-session', headers=guest_headers[0]).status_code == 401
        assert client.get('/api/auth/guest-session', headers=guest_headers[1]).status_code == 200


def test_cross_section_seat_write_and_camera_auth(school, monkeypatch):
    client, db, admin, one, two = school
    a, b = section(client, one), section(client, two)
    foreign = db.register_student(b['id'], 'Student B', 'B-1')
    foreign['section_id'] = a['id']
    assert client.put('/api/seats?section_id=' + a['id'], headers=one, json={'seats': [foreign]}).status_code == 404
    assert client.post('/api/camera/settings', headers=one, json={'model_name': '../../bad.pt'}).status_code == 422
    assert client.get('/api/analytics/grades?section_id=' + a['id'] + '&target=0', headers=one).status_code == 422
    monkeypatch.setattr(dashboard, 'EDGE_API_KEY', '')
    assert client.get('/api/seats?section_id=' + b['id']).status_code == 401


def test_migration_retires_plaintext_pin_preserving_records(tmp_path):
    path = str(tmp_path / 'legacy.db')
    db = DatabaseManager(path)
    sec = db.create_section('Historical class', 'Subject', 'Room', 'legacy-teacher')
    student = db.enroll_student(sec['id'], 'Existing Student', 'S-1')
    with db._connect() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'password_login_v1'")
        conn.execute("INSERT INTO teachers(id, name, pin, pin_hash, created_at) VALUES ('legacy-teacher', 'Legacy Teacher', '1234', 'old-hash', '2026-01-01')")
    migrated = DatabaseManager(path)
    assert migrated.get_student_by_id(student['id'])['name'] == 'Existing Student'
    assert migrated.get_section_owner(sec['id']) == 'legacy-teacher'
    with migrated._connect() as conn:
        teacher = conn.execute("SELECT * FROM teachers WHERE id = 'legacy-teacher'").fetchone()
        assert teacher['pin'] is None and teacher['pin_hash'] is None
    assert migrated.authenticate_teacher('LEGACY-TEACHER', '1234') is None


def test_browser_socket_isolation_and_credential_revocation(school):
    from starlette.websockets import WebSocketDisconnect
    client, db, admin, one, two = school
    a, b = section(client, one), section(client, two)
    seat_a = db.register_student(a['id'], 'Private A', 'A-1')
    db.register_student(b['id'], 'Private B', 'B-1')
    sa = client.post('/api/sessions/start', headers=one, json={'section_id': a['id']}).json()
    client.post('/api/sessions/start', headers=two, json={'section_id': b['id']})
    code = client.get('/api/sessions/active/guest-access', headers=one).json()['code']
    guest_token = client.post('/api/auth/guest', json={'code': code}).json()['token']
    with client.websocket_connect('/ws/events') as wa, client.websocket_connect('/ws/events') as wb, client.websocket_connect('/ws/events') as guest:
        wa.send_json({'teacher_token': one['X-Teacher-Token']})
        wb.send_json({'teacher_token': two['X-Teacher-Token']})
        guest.send_json({'guest_token': guest_token})
        assert 'Private B' not in str(wa.receive_json())
        wb.receive_json()
        guest.receive_json()
        client.post('/api/events/ingest', headers={'X-Edge-Key': 'edge-test'}, json=event(a, seat_a, sa, 'private-event'))
        message = wa.receive_json()
        assert len(message['ledger']) == 1 and 'Private B' not in str(message)
        public = guest.receive_json()
        assert len(public['ledger']) == 1 and 'student_id_number' not in str(public)
        wb.send_text('ping')
        assert wb.receive_text() == 'pong'
        teacher = db.get_teacher(db.get_section_owner(a['id']))
        response = client.put('/api/admin/teachers/' + teacher['id'], headers=admin,
                              json={'login_id': teacher['login_id'], 'name': teacher['name'], 'department': '', 'password': 'New-password-2026'})
        assert response.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            wa.receive_text()
        wb.send_text('ping')
        assert wb.receive_text() == 'pong'


def test_idle_teacher_cannot_read_foreign_roster_and_assignments(school):
    client, db, admin, one, two = school
    a, b = section(client, one), section(client, two)
    seat_a = db.register_student(a['id'], 'Same Name', 'A-1')
    student_b = db.enroll_student(b['id'], 'Same Name', 'B-1', photo_path='/uploads/students/private.jpg')
    assert client.get('/api/recitation/ledger', headers=one).json() == []
    assert len(client.get('/api/seats?section_id=' + a['id'], headers=one).json()) == 1
    assert client.get('/uploads/students/private.jpg', headers=one).status_code == 404
    assert client.post(f"/api/seats/{seat_a['id']}/assign?section_id={a['id']}", headers=one, json={'student_id': student_b['id']}).status_code == 409


def test_csv_names_are_literal_and_duplicate_reservations_roll_back(school):
    client, db, admin, one, two = school
    sec = section(client, one)
    path = f"/api/sections/{sec['id']}/students/enroll"
    payload = {'name': '=1+1', 'student_id_number': 'S-1', 'assign_to_seat_id': 'missing'}
    assert client.post(path, headers=one, json=payload).status_code == 409
    payload.pop('assign_to_seat_id')
    payload['auto_create_desk'] = True
    assert client.post(path, headers=one, json=payload).status_code == 200
    response = client.get('/api/exports/class-report.csv?section_id=' + sec['id'], headers=one)
    assert response.status_code == 200 and "'=1+1" in response.text
