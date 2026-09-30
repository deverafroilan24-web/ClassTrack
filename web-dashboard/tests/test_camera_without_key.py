"""An ordinary camera can sync names, gestures and controls without a shared key."""

from test_live_session import live_class, dashboard, event


def test_keyless_roster_session_event_and_skeleton_control(live_class):
    client, db, section, seat, token, session = live_class
    assert client.get('/api/sections').status_code == 200
    roster = client.get('/api/seats', params={'section_id': section['id']})
    assert roster.status_code == 200
    assert roster.json()[0]['student_name'] == 'Student One'
    assert roster.json()[0]['student_id'] == seat['student_id']
    assert client.get('/api/sessions/active').json()['id'] == session['id']
    with client.websocket_connect('/ws/edge') as camera:
        init = camera.receive_json()
        assert init['active_session']['id'] == session['id']
        assert init['selected_section_id'] == section['id']
        response = client.post('/api/camera/settings', headers={'X-Teacher-Token': token}, json={'show_skeleton': True})
        assert response.status_code == 200
        assert camera.receive_json()['settings']['show_skeleton'] is True
        settings = client.get('/api/camera/settings', params={'section_id': section['id']})
        assert settings.json()['settings']['show_skeleton'] is True
        assert client.post('/api/events/ingest', json=event(section, seat, session, 'keyless-raise')).status_code == 200
        assert client.get('/api/recitation/ledger', headers={'X-Teacher-Token': token}).json()[0]['latest_queue_pos'] == 1
    # Management actions continue to require a signed-in teacher.
    assert client.post('/api/camera/settings', json={'show_skeleton': False}).status_code == 401
    assert client.post('/api/sessions/start', json={'section_id': section['id']}).status_code == 401
    assert client.get('/api/admin/teachers').status_code == 401


def test_unbound_camera_receives_start_when_many_sections_exist(live_class):
    client, db, section, seat, token, session = live_class
    db.create_section('Other class', '', '', teacher_id='teacher_master')
    assert client.post('/api/sessions/stop', headers={'X-Teacher-Token': token}).status_code == 200
    with client.websocket_connect('/ws/edge') as camera:
        init = camera.receive_json()
        assert init['selected_section_id'] is None
        response = client.post('/api/sessions/start', headers={'X-Teacher-Token': token}, json={'section_id': section['id']})
        assert response.status_code == 201
        command = camera.receive_json()
        assert command['type'] == 'SESSION_STARTED'
        assert command['section_id'] == section['id']


def test_keyless_calibration_cannot_change_student_assignment(live_class):
    client, db, section, seat, token, session = live_class
    roster = client.get('/api/seats', params={'section_id': section['id']}).json()
    roster[0]['student_name'] = 'Forged assignment'
    response = client.put('/api/seats', params={'section_id': section['id']}, json={'seats': roster})
    assert response.status_code == 403
    assert db.get_seats(section_id=section['id'])[0]['student_name'] == 'Student One'
