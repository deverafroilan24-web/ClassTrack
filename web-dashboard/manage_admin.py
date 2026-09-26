"""Local, interactive administrator provisioning. No default/shared password."""
import getpass
import os
from pathlib import Path

from dotenv import load_dotenv
from backend.database import DatabaseManager


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    login_id = input('Administrator ID: ').strip().upper()
    import re
    if not re.fullmatch(r'[A-Z0-9._/-]{1,64}', login_id):
        raise SystemExit('Use 1–64 letters, digits, dots, hyphens, slashes or underscores.')
    email = input('Administrator email: ').strip()
    if not email:
        raise SystemExit('Administrator email is required.')
    password = getpass.getpass('New password (12–128 characters): ')
    if not 12 <= len(password) <= 128 or len(set(password)) < 4 or not password.strip():
        raise SystemExit('Choose a longer, varied password.')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords do not match.')
    db_path = Path(os.getenv('DATABASE_PATH', 'hand_tracking.db'))
    if not db_path.is_absolute():
        db_path = Path(__file__).parent / db_path
    database = DatabaseManager(str(db_path))
    try:
        database.save_admin(login_id, password, email)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print('Administrator saved. Open /admin and sign in. Existing administrator sessions have expired.')


if __name__ == '__main__':
    main()
