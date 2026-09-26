"""Real Chromium checks against an isolated local server and temporary database."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

playwright = pytest.importorskip('playwright.sync_api')
from test_live_session import DatabaseManager

PASSWORD = 'Browser-test-password-2026'


@pytest.fixture(scope='module')
def browser_server(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path_factory.mktemp('browser-school')
    database = DatabaseManager(str(directory / 'browser.db'))
    database.create_teacher('Administrator', '', 'ADMIN', PASSWORD, is_admin=True, account_id='teacher_master')
    teacher = database.create_teacher('Alex Rivera', 'Science', 'T-101', PASSWORD)
    database.create_section('Science 10-A', 'Physics', 'Room 204', teacher['id'])
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env = {**os.environ, 'DATABASE_URL': '', 'DATABASE_PATH': str(directory / 'browser.db'),
           'AUTH_SECRET': 'browser-test-signing-secret', 'EDGE_API_KEY': 'browser-edge-key'}
    log = (directory / 'server.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', str(port)],
                               cwd=root, env=env, stdout=log, stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + '/api/edge/status', timeout=1)
                break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail('Browser test server did not start')
        yield url, directory
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def login(page, account):
    page.locator('#teacher-id-input').fill(account)
    page.locator('#teacher-password-input').fill(PASSWORD)
    page.locator('#teacher-login-submit').click()
    playwright.expect(page.locator('#welcome-portal')).to_be_hidden()


def test_mobile_teacher_enrollment_and_session(browser_server):
    url, directory = browser_server
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/login')
        playwright.expect(page.locator('#teacher-id-input')).to_be_visible()
        page.screenshot(path=str(directory / 'mobile-login.png'), full_page=True)
        login(page, 'T-101')
        page.locator('[data-view="sections"]').click()
        page.locator('#btn-open-register-student').click()
        page.locator('#reg-student-name').fill("Maria O'Connor")
        page.locator('#reg-student-id').fill('2026-001')
        page.locator('#reg-auto-desk').check()
        page.locator('#btn-submit-enroll').click()
        playwright.expect(page.locator('#modal-register-student')).to_be_hidden()
        playwright.expect(page.locator('#section-roster-tbody')).to_contain_text("Maria O'Connor")
        page.locator('[data-view="seating"]').click()
        attendance = page.get_by_role('button', name='Mark Absent', exact=True).first
        playwright.expect(attendance).to_be_visible()
        assert attendance.evaluate("(button) => button.getBoundingClientRect().bottom <= button.closest('.attendance-desk').getBoundingClientRect().bottom")
        page.screenshot(path=str(directory / 'mobile-seating.png'), full_page=True)
        page.locator('[data-view="sections"]').click()
        page.locator('#btn-open-register-student').click()
        page.locator('#reg-student-name').fill("Maria O'Connor")
        page.locator('#reg-student-id').fill('2026-001')
        page.locator('#btn-submit-enroll').click()
        playwright.expect(page.locator('#toast-container')).to_contain_text('already enrolled')
        page.get_by_role('button', name='Cancel', exact=True).click()
        page.locator('[data-view="class"]').click()
        page.locator('#btn-banner-start').click()
        playwright.expect(page.locator('#btn-banner-stop')).to_be_visible()
        for width in (360, 390, 768, 1440):
            page.set_viewport_size({'width': width, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Overflow at {width}px'
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(directory / 'mobile-class.png'), full_page=True)
        page.locator('#btn-banner-stop').click()
        page.get_by_role('button', name='End Class Session', exact=True).last.click()
        playwright.expect(page.locator('#btn-banner-start')).to_be_visible()
        assert not errors, errors
        browser.close()


def test_admin_add_edit_delete_on_desktop_and_mobile(browser_server):
    url, directory = browser_server
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/admin')
        login(page, 'ADMIN')
        playwright.expect(page.locator('#view-teachers')).to_be_visible()
        assert page.locator('[data-view="class"]').count() == 0
        page.locator('#add-teacher').click()
        page.locator('#new-teacher-name').fill('Jordan Lee')
        page.locator('#new-teacher-department').fill('Mathematics')
        page.locator('#new-teacher-id').fill('T-202')
        page.locator('#new-teacher-password').fill(PASSWORD)
        page.locator('#teacher-create-submit').click()
        playwright.expect(page.locator('#teachers-table-body')).to_contain_text('Jordan Lee')
        row = page.locator('#teachers-table-body tr').filter(has_text='Jordan Lee')
        row.get_by_role('button', name='Edit', exact=True).click()
        page.locator('#new-teacher-name').fill('Jordan Lee Updated')
        page.locator('#teacher-create-submit').click()
        playwright.expect(page.locator('#teacher-admin-message')).to_contain_text('Credentials updated')
        page.screenshot(path=str(directory / 'desktop-admin.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(directory / 'mobile-admin.png'), full_page=True)
        row.get_by_role('button', name='Delete', exact=True).click()
        page.get_by_role('button', name='Delete account', exact=True).click()
        playwright.expect(page.locator('#teacher-admin-message')).to_contain_text('account deleted')
        playwright.expect(row).to_have_count(0)
        assert not errors, errors
        browser.close()
