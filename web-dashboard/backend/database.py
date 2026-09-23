import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    teacher_id TEXT
);

CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    section_id TEXT REFERENCES sections(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    student_id_number TEXT NOT NULL DEFAULT '',
    photo_path TEXT,
    face_embedding TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    section_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS seats (
    id TEXT PRIMARY KEY,
    section_id TEXT,
    label TEXT NOT NULL,
    student_name TEXT NOT NULL,
    student_id_number TEXT NOT NULL DEFAULT '',
    student_id TEXT REFERENCES students(id) ON DELETE SET NULL,
    grid_row INTEGER NOT NULL DEFAULT 0,
    grid_col INTEGER NOT NULL DEFAULT 0,
    x_min REAL NOT NULL,
    y_min REAL NOT NULL,
    x_max REAL NOT NULL,
    y_max REAL NOT NULL,
    is_present INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    seat_id TEXT,
    student_name TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    arm_angle REAL NOT NULL,
    duration_sec REAL NOT NULL,
    queue_pos INTEGER,
    timestamp_ms INTEGER NOT NULL,
    earned_point INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'VERIFIED',
    face_similarity REAL NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_seat ON events (seat_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_seats_section ON seats (section_id);
CREATE INDEX IF NOT EXISTS idx_students_section ON students (section_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_pin_hash(pin: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


class PgCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _serialize_row(self, r):
        if r is None:
            return None
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d

    def fetchone(self):
        r = self.cursor.fetchone()
        return self._serialize_row(r)

    def fetchall(self):
        rows = self.cursor.fetchall()
        return [self._serialize_row(r) for r in rows] if rows else []


class PgConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def _convert_query(self, sql: str) -> str:
        # Convert SQLite INSERT OR REPLACE INTO seats/events to PostgreSQL INSERT ... ON CONFLICT (id) DO UPDATE ...
        if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+seats\b", sql, re.IGNORECASE):
            sql = re.sub(
                r"INSERT\s+OR\s+REPLACE\s+INTO\s+seats\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
                r"INSERT INTO seats (\1) VALUES (\2) ON CONFLICT (id) DO UPDATE SET "
                r"section_id = EXCLUDED.section_id, label = EXCLUDED.label, "
                r"student_name = EXCLUDED.student_name, student_id_number = EXCLUDED.student_id_number, "
                r"student_id = EXCLUDED.student_id, grid_row = EXCLUDED.grid_row, "
                r"grid_col = EXCLUDED.grid_col, x_min = EXCLUDED.x_min, "
                r"y_min = EXCLUDED.y_min, x_max = EXCLUDED.x_max, "
                r"y_max = EXCLUDED.y_max, is_present = EXCLUDED.is_present",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
        elif re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+events\b", sql, re.IGNORECASE):
            sql = re.sub(
                r"INSERT\s+OR\s+REPLACE\s+INTO\s+events\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
                r"INSERT INTO events (\1) VALUES (\2) ON CONFLICT (id) DO UPDATE SET "
                r"session_id = EXCLUDED.session_id, seat_id = EXCLUDED.seat_id, "
                r"student_name = EXCLUDED.student_name, status = EXCLUDED.status, "
                r"reason_code = EXCLUDED.reason_code, arm_angle = EXCLUDED.arm_angle, "
                r"duration_sec = EXCLUDED.duration_sec, queue_pos = EXCLUDED.queue_pos, "
                r"timestamp_ms = EXCLUDED.timestamp_ms, earned_point = EXCLUDED.earned_point, "
                r"verification_status = EXCLUDED.verification_status, face_similarity = EXCLUDED.face_similarity",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )

        # Convert DATE(x) to (x)::date for PostgreSQL
        sql = re.sub(r"DATE\((.*?)\)", r"(\1)::date", sql, flags=re.IGNORECASE)

        # Convert ? to %s for psycopg2
        sql = sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Optional[Any] = None) -> PgCursorWrapper:
        cur = self.conn.cursor()
        converted = self._convert_query(sql)
        if params is not None:
            cur.execute(converted, params)
        else:
            cur.execute(converted)
        return PgCursorWrapper(cur)

    def executescript(self, sql_script: str) -> None:
        cur = self.conn.cursor()
        cur.execute(sql_script)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()


class DatabaseManager:
    def __init__(self, db_path: Optional[str] = None):
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        if self.database_url:
            print(f"[DatabaseManager] Using Supabase/PostgreSQL database via DATABASE_URL")
            self.is_postgres = True
        else:
            print(f"[DatabaseManager] Using local SQLite database")
            self.is_postgres = False
            self.db_path = Path(db_path or "hand_tracking.db")
            if self.db_path.parent != Path("."):
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.init_db()
        self.init_auth_schema()

    def init_auth_schema(self) -> None:
        """Idempotent auth migration for both SQLite and PostgreSQL."""
        timestamp_type = "TIMESTAMPTZ" if self.is_postgres else "TEXT"
        pin = os.getenv("TEACHER_PIN") or "1234"
        with self._connect() as conn:
            if self.is_postgres:
                conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS guest_code TEXT")
                conn.execute("ALTER TABLE sections ADD COLUMN IF NOT EXISTS teacher_id TEXT")
            else:
                if "guest_code" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
                    conn.execute("ALTER TABLE sessions ADD COLUMN guest_code TEXT")
                if "teacher_id" not in {row[1] for row in conn.execute("PRAGMA table_info(sections)")}:
                    conn.execute("ALTER TABLE sections ADD COLUMN teacher_id TEXT")
            for session in conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL AND guest_code IS NULL").fetchall():
                conn.execute("UPDATE sessions SET guest_code = ? WHERE id = ?",
                             (secrets.token_hex(4).upper(), session["id"]))
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at {timestamp_type} NOT NULL)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS teachers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, pin TEXT UNIQUE,
                department TEXT NOT NULL DEFAULT '', pin_hash TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                auth_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            )""")
            if self.is_postgres:
                conn.execute("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS pin TEXT")
                conn.execute("ALTER TABLE teachers ALTER COLUMN pin_hash DROP NOT NULL")
                conn.execute("ALTER TABLE teachers ALTER COLUMN is_active SET DEFAULT 1")
                conn.execute("ALTER TABLE teachers ALTER COLUMN auth_version SET DEFAULT 1")
                conn.execute("ALTER TABLE teachers ALTER COLUMN created_at SET DEFAULT NOW()")
                conn.execute("ALTER TABLE teachers ALTER COLUMN id SET DEFAULT ('teacher_' || replace(gen_random_uuid()::text, '-', ''))")
                conn.execute("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 1")
            else:
                if "pin" not in {row[1] for row in conn.execute("PRAGMA table_info(teachers)")}:
                    conn.execute("ALTER TABLE teachers ADD COLUMN pin TEXT")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_pin_unique ON teachers(pin) WHERE pin IS NOT NULL")
            # Preserve the existing sample accounts and section ownership during the
            # migration. New teacher accounts are managed only through the database.
            legacy = [
                ("teacher_1234", "Teacher Froilan", "Information Technology", "1234"),
                ("teacher_4321", "Teacher Leonard", "Computer Science", "4321"),
                ("teacher_1111", "Teacher Santos", "Engineering", "1111"),
                ("teacher_2222", "Teacher Garcia", "General Education", "2222"),
            ]
            # Migrate each legacy account independently. ON CONFLICT preserves
            # existing names, PIN changes, and deactivation state on later startups.
            for teacher_id, name, department, teacher_pin in legacy:
                conn.execute("UPDATE teachers SET pin = ? WHERE id = ? AND pin IS NULL",
                             (teacher_pin, teacher_id))
                conn.execute(
                    "INSERT INTO teachers (id, name, department, pin, pin_hash, is_active, auth_version, created_at) VALUES (?, ?, ?, ?, ?, 1, 1, ?) ON CONFLICT (id) DO NOTHING",
                    (teacher_id, name, department, teacher_pin, hash_pin(teacher_pin), _now_iso()),
                )
            existing = conn.execute("SELECT value FROM app_settings WHERE key = ?", ("teacher_pin_hash",)).fetchone()
            if existing and (not os.getenv("TEACHER_PIN") or verify_pin_hash(pin, existing["value"])):
                return
            pin_hash = hash_pin(pin)
            if existing:
                conn.execute("UPDATE app_settings SET value = ?, updated_at = ? WHERE key = ?",
                             (pin_hash, _now_iso(), "teacher_pin_hash"))
            else:
                conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
                             ("teacher_pin_hash", pin_hash, _now_iso()))

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def list_teachers(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, name, department, is_active, created_at FROM teachers ORDER BY name").fetchall()
            return [dict(row) for row in rows]

    def get_teacher_by_pin(self, pin: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, name, department, pin, pin_hash, auth_version FROM teachers WHERE is_active = 1").fetchall()
        for row in rows:
            if (row.get("pin") if isinstance(row, dict) else row["pin"]) == pin or verify_pin_hash(pin, row["pin_hash"]):
                return {"id": row["id"], "name": row["name"], "department": row["department"],
                        "auth_version": row["auth_version"]}
        return None

    def get_teacher(self, teacher_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT id, name, department, is_active, auth_version FROM teachers WHERE id = ?",
                               (teacher_id,)).fetchone()
            return dict(row) if row else None

    def create_teacher(self, name: str, department: str, pin: str) -> Dict[str, Any]:
        # Ensure a PIN cannot identify two accounts.
        with self._connect() as conn:
            existing = conn.execute("SELECT pin, pin_hash FROM teachers").fetchall()
            if any((row.get("pin") if isinstance(row, dict) else row["pin"]) == pin or verify_pin_hash(pin, row["pin_hash"])
                   for row in existing):
                raise ValueError("That teacher code is already assigned")
            teacher = {"id": f"teacher_{uuid.uuid4().hex[:12]}", "name": name.strip(),
                       "department": department.strip(), "is_active": 1, "created_at": _now_iso()}
            conn.execute(
                "INSERT INTO teachers (id, name, department, pin, pin_hash, is_active, auth_version, created_at) VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                (teacher["id"], teacher["name"], teacher["department"], pin, hash_pin(pin), teacher["created_at"]),
            )
        return teacher

    def set_teacher_active(self, teacher_id: str, active: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE teachers SET is_active = ?, auth_version = auth_version + 1 WHERE id = ?",
                                  (1 if active else 0, teacher_id))
            return cursor.rowcount > 0

    @contextmanager
    def _connect(self, foreign_keys: bool = True):
        if self.is_postgres:
            conn = psycopg2.connect(
                self.database_url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            conn.autocommit = True
            try:
                yield PgConnectionWrapper(conn)
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            if foreign_keys:
                conn.execute("PRAGMA foreign_keys = ON;")
            else:
                conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("PRAGMA journal_mode = WAL;")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def init_db(self) -> None:
        with self._connect(foreign_keys=False) as conn:
            # Migrate legacy sessions if needed
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            if cols and "label" in cols and "title" not in cols:
                conn.execute("CREATE TABLE sessions_new (id TEXT PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT);")
                conn.execute("INSERT OR IGNORE INTO sessions_new (id, title, started_at, ended_at) SELECT id, COALESCE(label, 'Session'), started_at, ended_at FROM sessions;")
                conn.execute("DROP TABLE sessions;")
                conn.execute("ALTER TABLE sessions_new RENAME TO sessions;")

            # Migrate legacy seats if needed
            seat_cols = {r[1] for r in conn.execute("PRAGMA table_info(seats)")}
            if seat_cols and "seat_label" in seat_cols and "x_min" not in seat_cols:
                conn.execute("CREATE TABLE seats_new (id TEXT PRIMARY KEY, label TEXT NOT NULL, student_name TEXT NOT NULL, student_id_number TEXT NOT NULL DEFAULT '', x_min REAL NOT NULL, y_min REAL NOT NULL, x_max REAL NOT NULL, y_max REAL NOT NULL, is_present INTEGER NOT NULL DEFAULT 1);")
                conn.execute("INSERT OR IGNORE INTO seats_new (id, label, student_name, student_id_number, x_min, y_min, x_max, y_max, is_present) SELECT id, COALESCE(seat_label, id), COALESCE(person_name, 'Student'), '', 0.05, 0.15, 0.45, 0.85, 1 FROM seats;")
                conn.execute("DROP TABLE seats;")
                conn.execute("ALTER TABLE seats_new RENAME TO seats;")

            # Ensure section_id in seats and sessions
            seat_cols = {r[1] for r in conn.execute("PRAGMA table_info(seats)")}
            if seat_cols and "section_id" not in seat_cols:
                conn.execute("ALTER TABLE seats ADD COLUMN section_id TEXT;")
            if seat_cols and "student_id" not in seat_cols:
                conn.execute("ALTER TABLE seats ADD COLUMN student_id TEXT;")
            if seat_cols and "grid_row" not in seat_cols:
                conn.execute("ALTER TABLE seats ADD COLUMN grid_row INTEGER NOT NULL DEFAULT 0;")
            if seat_cols and "grid_col" not in seat_cols:
                conn.execute("ALTER TABLE seats ADD COLUMN grid_col INTEGER NOT NULL DEFAULT 0;")

            sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            if sess_cols and "section_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN section_id TEXT;")

            # Ensure verification columns in events
            evt_cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            if evt_cols and "verification_status" not in evt_cols:
                conn.execute("ALTER TABLE events ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'VERIFIED';")
            if evt_cols and "face_similarity" not in evt_cols:
                conn.execute("ALTER TABLE events ADD COLUMN face_similarity REAL NOT NULL DEFAULT 1.0;")

            conn.executescript(SCHEMA_SQL)

            # Auto-link any existing seats that have student_name matching a student in that section
            conn.execute(
                """
                UPDATE seats
                SET student_id = (
                    SELECT st.id FROM students st 
                    WHERE (st.section_id = seats.section_id OR seats.section_id IS NULL) 
                      AND st.name = seats.student_name 
                    LIMIT 1
                )
                WHERE student_id IS NULL 
                  AND student_name NOT LIKE 'Empty%' 
                  AND student_name != '';
                """
            )

    # -------------------------------------------------------------
    # SECTIONS
    # -------------------------------------------------------------
    def get_sections(self, teacher_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            query = """
                SELECT 
                    sec.id, sec.name, sec.subject, sec.room, sec.created_at, sec.teacher_id,
                    COUNT(DISTINCT s.id) as student_count,
                    COUNT(DISTINCT sess.id) as session_count
                FROM sections sec
                LEFT JOIN seats s ON sec.id = s.section_id
                LEFT JOIN sessions sess ON sec.id = sess.section_id
            """
            params = ()
            if teacher_id:
                query += " WHERE COALESCE(sec.teacher_id, 'teacher_master') = ? "
                params = (teacher_id,)
            query += """
                GROUP BY sec.id
                ORDER BY sec.name ASC
            """
            cursor = conn.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def create_section(self, name: str, subject: str, room: str, teacher_id: Optional[str] = None) -> Dict[str, Any]:
        sec_id = f"sec_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sections (id, name, subject, room, created_at, teacher_id) VALUES (?, ?, ?, ?, ?, ?)",
                (sec_id, name, subject, room, now, teacher_id),
            )
        return {"id": sec_id, "name": name, "subject": subject, "room": room, "created_at": now, "teacher_id": teacher_id}

    def get_section_owner(self, section_id: Optional[str]) -> Optional[str]:
        if not section_id:
            return "teacher_master"
        with self._connect() as conn:
            row = conn.execute("SELECT teacher_id FROM sections WHERE id = ?", (section_id,)).fetchone()
            if not row:
                return None
            return row["teacher_id"] or "teacher_master"

    def delete_section(self, section_id: str) -> bool:
        with self._connect(foreign_keys=False) as conn:
            conn.execute("DELETE FROM events WHERE session_id IN (SELECT id FROM sessions WHERE section_id = ?)", (section_id,))
            conn.execute("DELETE FROM sessions WHERE section_id = ?", (section_id,))
            conn.execute("DELETE FROM seats WHERE section_id = ?", (section_id,))
            conn.execute("DELETE FROM students WHERE section_id = ?", (section_id,))
            conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
        return True

    def enroll_student(
        self,
        section_id: str,
        name: str,
        student_id_number: str = "",
        photo_path: str = "",
        face_embedding: str = "",
        assign_to_seat_id: Optional[str] = None,
        auto_create_desk: bool = False,
    ) -> Dict[str, Any]:
        student_id = f"stud_{uuid.uuid4().hex[:8]}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO students (id, section_id, name, student_id_number, photo_path, face_embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (student_id, section_id, name, student_id_number, photo_path, face_embedding, now),
            )
            if assign_to_seat_id:
                conn.execute(
                    """
                    UPDATE seats SET 
                        student_id = ?,
                        student_name = ?,
                        student_id_number = ?
                    WHERE id = ?
                    """,
                    (student_id, name, student_id_number, assign_to_seat_id),
                )
            elif auto_create_desk:
                # Find current maximum seat count or empty seats
                cursor = conn.execute("SELECT COUNT(*) as cnt FROM seats WHERE section_id = ?", (section_id,))
                cur_count = cursor.fetchone()["cnt"]
                
                # Check if there is an existing empty seat to claim first
                empty_cursor = conn.execute(
                    "SELECT id FROM seats WHERE section_id = ? AND (student_id IS NULL OR student_name LIKE 'Empty%') ORDER BY grid_row ASC, grid_col ASC LIMIT 1",
                    (section_id,)
                )
                first_empty = empty_cursor.fetchone()
                if first_empty:
                    conn.execute(
                        """
                        UPDATE seats SET
                            student_id = ?,
                            student_name = ?,
                            student_id_number = ?
                        WHERE id = ?
                        """,
                        (student_id, name, student_id_number, first_empty["id"]),
                    )
                else:
                    # Dynamically append a new seat
                    import math
                    new_idx = cur_count + 1
                    cols = max(3, math.ceil(math.sqrt(new_idx)))
                    r = (new_idx - 1) // cols
                    c = (new_idx - 1) % cols
                    row_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    row_label = row_letters[r] if r < len(row_letters) else f"R{r+1}"
                    label = f"Desk {row_label}{c+1}"
                    seat_id = f"seat_{section_id}_{row_label}{c+1}"

                    col_w = 1.0 / max(cols, 1)
                    row_h = 1.0 / max(r + 1, 1)
                    x_min = round(c * col_w + col_w * 0.04, 3)
                    x_max = round((c + 1) * col_w - col_w * 0.04, 3)
                    y_min = round(r * row_h + row_h * 0.04, 3)
                    y_max = round((r + 1) * row_h - row_h * 0.04, 3)

                    conn.execute(
                        """
                        INSERT INTO seats (id, section_id, label, student_name, student_id_number, student_id, grid_row, grid_col, x_min, y_min, x_max, y_max, is_present)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (seat_id, section_id, label, name, student_id_number, student_id, r, c, x_min, y_min, x_max, y_max),
                    )

            cursor = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,))
            return dict(cursor.fetchone())

    def get_students(self, section_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            query = """
                SELECT 
                    st.id, st.section_id, st.name, st.student_id_number, st.photo_path,
                    (st.face_embedding IS NOT NULL AND st.face_embedding != '') as has_face_model,
                    st.created_at,
                    s.id as assigned_seat_id,
                    s.label as assigned_seat_label,
                    COALESCE(SUM(CASE WHEN e.earned_point = 1 THEN 1 ELSE 0 END), 0) as total_points,
                    COUNT(e.id) as total_raises
                FROM students st
                LEFT JOIN seats s ON (st.id = s.student_id OR (s.student_id IS NULL AND s.student_name = st.name))
                LEFT JOIN events e ON (s.id = e.seat_id OR st.name = e.student_name)
            """
            params: List[Any] = []
            if section_id:
                query += " WHERE st.section_id = ?"
                params.append(section_id)
            query += " GROUP BY st.id, st.section_id, st.name, st.student_id_number, st.photo_path, st.face_embedding, st.created_at, s.id, s.label ORDER BY st.name ASC"

            cursor = conn.execute(query, params)
            return [
                {
                    **dict(r),
                    "has_face_model": bool(r["has_face_model"]),
                }
                for r in cursor.fetchall()
            ]

    def get_student_by_id(self, student_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_student(self, student_id: str) -> bool:
        with self._connect(foreign_keys=False) as conn:
            conn.execute(
                """
                UPDATE seats SET 
                    student_id = NULL,
                    student_name = 'Empty (' || label || ')',
                    student_id_number = ''
                WHERE student_id = ?
                """,
                (student_id,),
            )
            conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
        return True

    def register_student(
        self,
        section_id: str,
        student_name: str,
        student_id_number: str,
        seat_id: Optional[str] = None,
        label: Optional[str] = None,
        photo_path: str = "",
        face_embedding: str = "",
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT id FROM students WHERE name = ? AND section_id = ?",
                (student_name, section_id),
            )
            stud = cursor.fetchone()
            if stud:
                stud_id = stud["id"]
                conn.execute(
                    "UPDATE students SET student_id_number = ?, photo_path = COALESCE(NULLIF(?, ''), photo_path), face_embedding = COALESCE(NULLIF(?, ''), face_embedding) WHERE id = ?",
                    (student_id_number, photo_path, face_embedding, stud_id),
                )
            else:
                stud_id = f"stud_{uuid.uuid4().hex[:8]}"
                conn.execute(
                    "INSERT INTO students (id, section_id, name, student_id_number, photo_path, face_embedding, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (stud_id, section_id, student_name, student_id_number, photo_path, face_embedding, _now_iso()),
                )

            if seat_id:
                # Update existing seat
                conn.execute(
                    """
                    UPDATE seats SET 
                        student_id = ?,
                        student_name = ?, 
                        student_id_number = ?,
                        section_id = ?
                    WHERE id = ?
                    """,
                    (stud_id, student_name, student_id_number, section_id, seat_id),
                )
                actual_seat_id = seat_id
            else:
                # Create a new seat in section
                actual_seat_id = f"seat_{uuid.uuid4().hex[:6]}"
                actual_label = label or f"Desk {actual_seat_id[-2:].upper()}"
                conn.execute(
                    """
                    INSERT INTO seats (id, section_id, label, student_name, student_id_number, student_id, x_min, y_min, x_max, y_max, is_present)
                    VALUES (?, ?, ?, ?, ?, ?, 0.10, 0.20, 0.45, 0.80, 1)
                    """,
                    (actual_seat_id, section_id, actual_label, student_name, student_id_number, stud_id),
                )
            cursor = conn.execute("SELECT * FROM seats WHERE id = ?", (actual_seat_id,))
            return dict(cursor.fetchone())

    # -------------------------------------------------------------
    # SESSIONS
    # -------------------------------------------------------------
    def start_session(self, title: str = "Classroom Recitation Session", section_id: Optional[str] = None) -> Dict[str, Any]:
        session_id = f"sess_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        started_at = _now_iso()
        guest_code = secrets.token_hex(4).upper()
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL", (started_at,))
            conn.execute(
                "INSERT INTO sessions (id, title, section_id, started_at, ended_at, guest_code) VALUES (?, ?, ?, ?, NULL, ?)",
                (session_id, title, section_id, started_at, guest_code),
            )
        return {"id": session_id, "title": title, "section_id": section_id, "started_at": started_at, "ended_at": None,
                "guest_code": guest_code}

    def stop_session(self, session_id: Optional[str] = None, teacher_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        ended_at = _now_iso()
        with self._connect() as conn:
            if session_id:
                cursor = conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ? AND (? IS NULL OR COALESCE((SELECT teacher_id FROM sections WHERE sections.id = sessions.section_id), 'teacher_master') = ?) RETURNING *",
                    (ended_at, session_id, teacher_id, teacher_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL AND (? IS NULL OR COALESCE((SELECT teacher_id FROM sections WHERE sections.id = sessions.section_id), 'teacher_master') = ?) RETURNING *",
                    (ended_at, teacher_id, teacher_id),
                )
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def get_active_session(self, teacher_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT s.* FROM sessions s LEFT JOIN sections sec ON sec.id = s.section_id "
                "WHERE s.ended_at IS NULL AND (? IS NULL OR COALESCE(sec.teacher_id, 'teacher_master') = ?) "
                "ORDER BY s.started_at DESC LIMIT 1",
                (teacher_id, teacher_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_sessions(
        self,
        section_id: Optional[str] = None,
        date_filter: Optional[str] = None,
        teacher_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            q = """
                SELECT 
                    s.id, s.title, s.section_id, s.started_at, s.ended_at,
                    sec.name as section_name, sec.subject as section_subject,
                    COUNT(CASE WHEN e.status = 'VALID' THEN 1 END) as total_raises,
                    COUNT(CASE WHEN e.earned_point = 1 THEN 1 END) as total_points,
                    COUNT(DISTINCT CASE WHEN e.status = 'VALID' THEN e.seat_id END) as active_students
                FROM sessions s
                LEFT JOIN sections sec ON s.section_id = sec.id
                LEFT JOIN events e ON s.id = e.session_id
            """
            conditions = []
            p: List[Any] = []
            if section_id:
                conditions.append("(s.section_id = ? OR s.section_id IS NULL)")
                p.append(section_id)
            if date_filter:
                conditions.append("DATE(s.started_at) = DATE(?)")
                p.append(date_filter)
            if teacher_id:
                conditions.append("COALESCE(sec.teacher_id, 'teacher_master') = ?")
                p.append(teacher_id)

            if conditions:
                q += " WHERE " + " AND ".join(conditions)

            q += " GROUP BY s.id, s.title, s.section_id, s.started_at, s.ended_at, sec.name, sec.subject ORDER BY s.started_at DESC"
            cursor = conn.execute(q, p)
            return [dict(r) for r in cursor.fetchall()]

    def get_session_details(self, session_id: str, teacher_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT s.*, sec.name as section_name, sec.subject as section_subject
                FROM sessions s
                LEFT JOIN sections sec ON s.section_id = sec.id
                WHERE s.id = ? AND (? IS NULL OR COALESCE(sec.teacher_id, 'teacher_master') = ?)
                """,
                (session_id, teacher_id, teacher_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            sess = dict(row)
            ledger = self.get_recitation_ledger(session_id=session_id, section_id=sess.get("section_id"))
            sess["ledger"] = ledger
            return sess

    def get_event_context(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(sec.teacher_id, 'teacher_master') AS teacher_id, s.section_id, s.id AS session_id "
                "FROM events e JOIN sessions s ON s.id = e.session_id "
                "LEFT JOIN sections sec ON sec.id = s.section_id WHERE e.id = ?",
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_session(self, session_id: str, teacher_id: Optional[str] = None) -> bool:
        with self._connect(foreign_keys=False) as conn:
            if teacher_id and not conn.execute(
                "SELECT 1 FROM sessions s LEFT JOIN sections sec ON sec.id = s.section_id "
                "WHERE s.id = ? AND COALESCE(sec.teacher_id, 'teacher_master') = ?",
                (session_id, teacher_id),
            ).fetchone():
                return False
            conn.execute("DELETE FROM events WHERE session_id = ?;", (session_id,))
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
            return cursor.rowcount > 0

    def clear_all_sessions(self, section_id: Optional[str] = None, teacher_id: Optional[str] = None) -> int:
        with self._connect(foreign_keys=False) as conn:
            if teacher_id and section_id:
                owned = conn.execute(
                    "SELECT 1 FROM sections WHERE id = ? AND COALESCE(teacher_id, 'teacher_master') = ?",
                    (section_id, teacher_id),
                ).fetchone()
                if not owned:
                    return 0
                conn.execute(
                    "DELETE FROM events WHERE session_id IN (SELECT id FROM sessions WHERE section_id = ?);",
                    (section_id,),
                )
                cursor = conn.execute("DELETE FROM sessions WHERE section_id = ?;", (section_id,))
            elif section_id:
                conn.execute(
                    "DELETE FROM events WHERE session_id IN (SELECT id FROM sessions WHERE section_id = ?);",
                    (section_id,),
                )
                cursor = conn.execute("DELETE FROM sessions WHERE section_id = ?;", (section_id,))
            elif teacher_id:
                owner_filter = "SELECT s.id FROM sessions s LEFT JOIN sections sec ON sec.id = s.section_id WHERE COALESCE(sec.teacher_id, 'teacher_master') = ?"
                conn.execute(f"DELETE FROM events WHERE session_id IN ({owner_filter});", (teacher_id,))
                cursor = conn.execute(f"DELETE FROM sessions WHERE id IN ({owner_filter});", (teacher_id,))
            else:
                conn.execute("DELETE FROM events;")
                cursor = conn.execute("DELETE FROM sessions;")
            return cursor.rowcount

    # -------------------------------------------------------------
    # SEATS
    # -------------------------------------------------------------
    def get_seats(self, section_id: Optional[str] = None, teacher_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            query = """
                SELECT 
                    s.id, s.section_id, s.label,
                    COALESCE(st.name, s.student_name) as student_name,
                    COALESCE(st.student_id_number, s.student_id_number) as student_id_number,
                    COALESCE(s.student_id, st.id, (
                        SELECT st2.id FROM students st2 
                        WHERE (st2.section_id = s.section_id OR s.section_id IS NULL) 
                          AND st2.name = s.student_name 
                        LIMIT 1
                    )) as student_id,
                    COALESCE(st.photo_path, '') as photo_path,
                    st.face_embedding,
                    COALESCE(s.grid_row, 0) as grid_row,
                    COALESCE(s.grid_col, 0) as grid_col,
                    s.x_min, s.y_min, s.x_max, s.y_max,
                    s.is_present,
                    COALESCE(SUM(CASE WHEN e.earned_point = 1 THEN 1 ELSE 0 END), 0) as total_points
                FROM seats s
                LEFT JOIN students st ON (s.student_id = st.id OR (s.student_id IS NULL AND s.student_name = st.name))
                LEFT JOIN events e ON s.id = e.seat_id
            """
            params: List[Any] = []
            conditions = []
            if section_id:
                conditions.append("s.section_id = ?")
                params.append(section_id)
            if teacher_id:
                conditions.append("COALESCE((SELECT sec.teacher_id FROM sections sec WHERE sec.id = s.section_id), 'teacher_master') = ?")
                params.append(teacher_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY s.id, s.section_id, s.label, s.student_name, s.student_id_number, s.student_id, s.grid_row, s.grid_col, s.x_min, s.y_min, s.x_max, s.y_max, s.is_present, st.id, st.name, st.student_id_number, st.photo_path, st.face_embedding ORDER BY s.grid_row ASC, s.grid_col ASC, s.label ASC"

            cursor = conn.execute(query, params)
            results = []
            for r in cursor.fetchall():
                d = dict(r)
                d["is_present"] = bool(d.get("is_present", 1))
                d["total_points"] = int(d.get("total_points", 0))
                d["grid_row"] = int(d.get("grid_row", 0) or 0)
                d["grid_col"] = int(d.get("grid_col", 0) or 0)
                d["has_photo"] = bool(d.get("photo_path"))
                results.append(d)
            return results

    def configure_seating_grid(self, section_id: str, rows: int, cols: int) -> List[Dict[str, Any]]:
        """
        Reconfigures or resizes a section's seating grid to (rows x cols).
        Maintains existing students where possible or creates clean desks.
        Automatically maps perspective-safe bounding boxes (x_min, y_min, x_max, y_max).
        """
        rows = max(1, min(10, int(rows)))
        cols = max(1, min(10, int(cols)))

        with self._connect(foreign_keys=False) as conn:
            cursor = conn.execute(
                "SELECT id, student_name, student_id_number, student_id FROM seats WHERE section_id = ? ORDER BY label ASC",
                (section_id,),
            )
            existing_assigned = [dict(r) for r in cursor.fetchall()]

            conn.execute("DELETE FROM seats WHERE section_id = ?", (section_id,))

            row_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            col_w = 1.0 / cols
            row_h = 1.0 / rows
            margin_x = col_w * 0.04
            margin_y = row_h * 0.04

            idx = 0
            for r in range(rows):
                row_label = row_letters[r] if r < len(row_letters) else f"R{r+1}"
                for c in range(cols):
                    seat_id = f"seat_{section_id}_{row_label}{c+1}"
                    label = f"Desk {row_label}{c+1}"

                    x_min = round(c * col_w + margin_x, 3)
                    x_max = round((c + 1) * col_w - margin_x, 3)
                    y_min = round(r * row_h + margin_y, 3)
                    y_max = round((r + 1) * row_h - margin_y, 3)

                    stud_name = f"Empty ({row_label}{c+1})"
                    stud_id_num = ""
                    stud_id = None
                    if idx < len(existing_assigned):
                        prev = existing_assigned[idx]
                        if prev.get("student_name") and not prev["student_name"].startswith("Empty"):
                            stud_name = prev["student_name"]
                            stud_id_num = prev.get("student_id_number", "")
                            stud_id = prev.get("student_id")

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO seats (id, section_id, label, student_name, student_id_number, student_id, grid_row, grid_col, x_min, y_min, x_max, y_max, is_present)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (seat_id, section_id, label, stud_name, stud_id_num, stud_id, r, c, x_min, y_min, x_max, y_max),
                    )
                    idx += 1

            conn.execute(
                """
                UPDATE seats 
                SET student_id = (SELECT id FROM students WHERE students.name = seats.student_name AND students.section_id = seats.section_id LIMIT 1)
                WHERE student_id IS NULL AND student_name NOT LIKE 'Empty%'
                """
            )

        return self.get_seats(section_id)

    def assign_student_to_seat(self, seat_id: str, student_id: Optional[str]) -> Dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT label, section_id FROM seats WHERE id = ?", (seat_id,))
            seat_row = cursor.fetchone()
            if not seat_row:
                raise KeyError(f"Seat {seat_id} not found")

            seat_label = seat_row["label"]
            sec_id = seat_row["section_id"]

            if not student_id:
                conn.execute(
                    """
                    UPDATE seats SET 
                        student_id = NULL,
                        student_name = ?,
                        student_id_number = ''
                    WHERE id = ?
                    """,
                    (f"Empty ({seat_label})", seat_id),
                )
            else:
                cursor = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                stud = cursor.fetchone()
                if not stud:
                    raise ValueError(f"Student {student_id} not found")

                # If student is already assigned to another seat in same section, unassign the other seat
                conn.execute(
                    """
                    UPDATE seats SET 
                        student_id = NULL,
                        student_name = 'Empty (' || label || ')',
                        student_id_number = ''
                    WHERE student_id = ? AND id != ?
                    """,
                    (student_id, seat_id),
                )

                conn.execute(
                    """
                    UPDATE seats SET 
                        student_id = ?,
                        student_name = ?,
                        student_id_number = ?
                    WHERE id = ?
                    """,
                    (stud["id"], stud["name"], stud["student_id_number"], seat_id),
                )

            cursor = conn.execute("SELECT * FROM seats WHERE id = ?", (seat_id,))
            return dict(cursor.fetchone())

    def swap_seats(self, seat_id_1: str, seat_id_2: str) -> bool:
        with self._connect() as conn:
            c1 = conn.execute("SELECT student_id, student_name, student_id_number FROM seats WHERE id = ?", (seat_id_1,)).fetchone()
            c2 = conn.execute("SELECT student_id, student_name, student_id_number FROM seats WHERE id = ?", (seat_id_2,)).fetchone()
            if not c1 or not c2:
                return False
            conn.execute(
                "UPDATE seats SET student_id = ?, student_name = ?, student_id_number = ? WHERE id = ?",
                (c2["student_id"], c2["student_name"], c2["student_id_number"], seat_id_1),
            )
            conn.execute(
                "UPDATE seats SET student_id = ?, student_name = ?, student_id_number = ? WHERE id = ?",
                (c1["student_id"], c1["student_name"], c1["student_id_number"], seat_id_2),
            )
            return True

    def auto_fill_seats_alphabetical(self, section_id: str) -> List[Dict[str, Any]]:
        """
        Auto-assigns all students in the section to the available seats alphabetically by last name.
        Seats are ordered by grid_row ASC, grid_col ASC, label ASC.
        """
        with self._connect(foreign_keys=False) as conn:
            cursor = conn.execute(
                "SELECT id, name, student_id_number FROM students WHERE section_id = ? ORDER BY name ASC",
                (section_id,),
            )
            students = [dict(r) for r in cursor.fetchall()]

            def get_last_name(name_str: str) -> str:
                parts = (name_str or "").strip().split()
                if not parts:
                    return ""
                if "," in name_str:
                    return parts[0].replace(",", "").lower()
                return parts[-1].lower()

            students.sort(key=lambda st: (get_last_name(st["name"]), st["name"].lower()))

            cursor = conn.execute(
                "SELECT id, label FROM seats WHERE section_id = ? ORDER BY grid_row ASC, grid_col ASC, label ASC",
                (section_id,),
            )
            seats = [dict(r) for r in cursor.fetchall()]

            for i, seat in enumerate(seats):
                if i < len(students):
                    st = students[i]
                    conn.execute(
                        """
                        UPDATE seats SET
                            student_id = ?,
                            student_name = ?,
                            student_id_number = ?
                        WHERE id = ?
                        """,
                        (st["id"], st["name"], st["student_id_number"], seat["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE seats SET
                            student_id = NULL,
                            student_name = ?,
                            student_id_number = ''
                        WHERE id = ?
                        """,
                        (f"Empty ({seat['label']})", seat["id"]),
                    )

    def auto_generate_desks_from_enrolled_students(self, section_id: str) -> List[Dict[str, Any]]:
        """
        Calculates an optimal grid based on exactly how many students are enrolled in the section.
        Generates and assigns only as many desks as enrolled students, eliminating unnecessary 'Empty' desks.
        """
        import math
        with self._connect(foreign_keys=False) as conn:
            cursor = conn.execute(
                "SELECT id, name, student_id_number, photo_path, face_embedding FROM students WHERE section_id = ? ORDER BY name ASC",
                (section_id,),
            )
            students = [dict(r) for r in cursor.fetchall()]

            def get_last_name(name_str: str) -> str:
                parts = (name_str or "").strip().split()
                if not parts:
                    return ""
                if "," in name_str:
                    return parts[0].replace(",", "").lower()
                return parts[-1].lower()

            students.sort(key=lambda st: (get_last_name(st["name"]), st["name"].lower()))

            # Clear existing desks in section
            conn.execute("DELETE FROM seats WHERE section_id = ?", (section_id,))

            n = len(students)
            if n == 0:
                return []

            # Determine balanced rows and columns:
            # e.g., 1-4 students: 2x2, 5-6: 2x3, 7-9: 3x3, 10-12: 3x4, etc.
            cols = max(2, math.ceil(math.sqrt(n * 1.3)))
            rows = math.ceil(n / cols)

            row_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            col_w = 1.0 / cols
            row_h = 1.0 / max(rows, 1)
            margin_x = col_w * 0.04
            margin_y = row_h * 0.04

            for idx, st in enumerate(students):
                r = idx // cols
                c = idx % cols
                row_label = row_letters[r] if r < len(row_letters) else f"R{r+1}"
                label = f"Desk {row_label}{c+1}"
                seat_id = f"seat_{section_id}_{row_label}{c+1}"

                x_min = round(c * col_w + margin_x, 3)
                x_max = round((c + 1) * col_w - margin_x, 3)
                y_min = round(r * row_h + margin_y, 3)
                y_max = round((r + 1) * row_h - margin_y, 3)

                conn.execute(
                    """
                    INSERT INTO seats (id, section_id, label, student_name, student_id_number, student_id, grid_row, grid_col, x_min, y_min, x_max, y_max, is_present)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (seat_id, section_id, label, st["name"], st["student_id_number"], st["id"], r, c, x_min, y_min, x_max, y_max),
                )

        return self.get_seats(section_id)

    def clear_seat_assignments(self, section_id: str) -> List[Dict[str, Any]]:
        """
        Clears all student assignments from all seats in the section, returning them to the pool.
        """
        with self._connect(foreign_keys=False) as conn:
            cursor = conn.execute("SELECT id, label FROM seats WHERE section_id = ?", (section_id,))
            for r in cursor.fetchall():
                conn.execute(
                    "UPDATE seats SET student_id = NULL, student_name = ?, student_id_number = '' WHERE id = ?",
                    (f"Empty ({r['label']})", r["id"]),
                )
        return self.get_seats(section_id)

    def get_participation_heatmap(self, section_id: Optional[str] = None) -> Dict[str, Any]:
        with self._connect() as conn:
            seats = self.get_seats(section_id)
            if not seats:
                return {"cells": [], "rows": 0, "cols": 0, "max_raises": 0}

            max_row = max((s.get("grid_row", 0) for s in seats), default=0) + 1
            max_col = max((s.get("grid_col", 0) for s in seats), default=0) + 1

            query = """
                SELECT 
                    seat_id,
                    COUNT(id) as total_raises,
                    COALESCE(SUM(CASE WHEN earned_point = 1 THEN 1 ELSE 0 END), 0) as total_points,
                    COALESCE(SUM(CASE WHEN verification_status = 'SEAT_MISMATCH' THEN 1 ELSE 0 END), 0) as mismatch_count
                FROM events
            """
            params: List[Any] = []
            if section_id:
                query += " WHERE session_id IN (SELECT id FROM sessions WHERE section_id = ?)"
                params.append(section_id)
            query += " GROUP BY seat_id"

            stats = {r["seat_id"]: dict(r) for r in conn.execute(query, params).fetchall()}

            max_raises = 1
            cells = []
            for s in seats:
                st = stats.get(s["id"], {"total_raises": 0, "total_points": 0, "mismatch_count": 0})
                r_count = st["total_raises"]
                if r_count > max_raises:
                    max_raises = r_count

                cells.append({
                    "seat_id": s["id"],
                    "label": s["label"],
                    "student_name": s["student_name"],
                    "student_id": s.get("student_id"),
                    "photo_path": s.get("photo_path"),
                    "grid_row": s.get("grid_row", 0),
                    "grid_col": s.get("grid_col", 0),
                    "total_raises": r_count,
                    "total_points": st["total_points"],
                    "mismatch_count": st["mismatch_count"],
                    "intensity": round(r_count / max_raises, 2) if max_raises > 0 else 0.0,
                })

            return {
                "section_id": section_id,
                "rows": max_row,
                "cols": max_col,
                "max_raises": max_raises,
                "cells": cells,
            }

    def calculate_participation_grades(
        self, section_id: str, target_raises: int = 5, weight_percent: float = 100.0
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT 
                    st.id as student_id,
                    st.name as student_name,
                    st.student_id_number,
                    st.photo_path,
                    MAX(s.label) as assigned_seat_label,
                    COUNT(DISTINCT CASE WHEN e.status = 'VALID' AND e.reason_code = 'VALID_HAND_RAISE' THEN e.id END) as total_raises,
                    COUNT(DISTINCT CASE WHEN e.earned_point = 1 THEN e.id END) as total_points,
                    COUNT(DISTINCT CASE WHEN e.verification_status = 'SEAT_MISMATCH' THEN e.id END) as seat_mismatches
                FROM students st
                LEFT JOIN seats s ON st.id = s.student_id AND s.section_id = st.section_id
                LEFT JOIN events e ON (s.id = e.seat_id OR (s.id IS NULL AND st.name = e.student_name))
                    AND e.session_id IN (SELECT id FROM sessions WHERE section_id = st.section_id)
                WHERE st.section_id = ?
                GROUP BY st.id
                ORDER BY st.name ASC
                """,
                (section_id,),
            )
            rows = cursor.fetchall()
            results = []
            for r in rows:
                pts = r["total_points"]
                target = max(1, target_raises)
                raw_score = min(1.0, pts / target) * 100.0
                weighted_grade = round(raw_score * (weight_percent / 100.0), 1)

                results.append({
                    "student_id": r["student_id"],
                    "student_name": r["student_name"],
                    "student_id_number": r["student_id_number"],
                    "photo_path": r["photo_path"],
                    "assigned_seat_label": r["assigned_seat_label"] or "Unassigned",
                    "total_raises": r["total_raises"],
                    "total_points": pts,
                    "seat_mismatches": r["seat_mismatches"],
                    "target_raises": target,
                    "participation_percentage": round(raw_score, 1),
                    "recitation_grade": weighted_grade,
                })
            return results

    def bulk_update_seats(self, seats: List[Dict[str, Any]], section_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect(foreign_keys=False) as conn:
            for s in seats:
                sec = s.get("section_id") or section_id or ""
                stud_id = s.get("student_id")
                # If student_id not explicitly given but student_name is not empty/placeholder, try lookup
                if not stud_id and s.get("student_name") and not s["student_name"].startswith("Empty ("):
                    cur = conn.execute("SELECT id FROM students WHERE name = ? AND (section_id = ? OR section_id IS NULL) LIMIT 1", (s["student_name"], sec))
                    row = cur.fetchone()
                    if row:
                        stud_id = row["id"]

                conn.execute(
                    """
                    INSERT INTO seats (id, section_id, label, student_id, student_name, student_id_number, x_min, y_min, x_max, y_max, is_present, grid_row, grid_col)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        section_id = COALESCE(excluded.section_id, seats.section_id),
                        label = excluded.label,
                        student_id = excluded.student_id,
                        student_name = excluded.student_name,
                        student_id_number = excluded.student_id_number,
                        x_min = excluded.x_min,
                        y_min = excluded.y_min,
                        x_max = excluded.x_max,
                        y_max = excluded.y_max,
                        is_present = excluded.is_present,
                        grid_row = COALESCE(excluded.grid_row, seats.grid_row),
                        grid_col = COALESCE(excluded.grid_col, seats.grid_col);
                    """,
                    (
                        s["id"],
                        sec,
                        s["label"],
                        stud_id,
                        s["student_name"],
                        s.get("student_id_number", ""),
                        float(s["x_min"]),
                        float(s["y_min"]),
                        float(s["x_max"]),
                        float(s["y_max"]),
                        1 if s.get("is_present", True) else 0,
                        int(s.get("grid_row", 0) or 0),
                        int(s.get("grid_col", 0) or 0),
                    ),
                )
        return self.get_seats(section_id=section_id)

    def set_section_attendance(self, section_id: str, is_present: bool) -> List[Dict[str, Any]]:
        val = 1 if is_present else 0
        with self._connect() as conn:
            conn.execute("UPDATE seats SET is_present = ? WHERE section_id = ?", (val, section_id))
        return self.get_seats(section_id=section_id)

    def toggle_attendance(self, seat_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT is_present FROM seats WHERE id = ?", (seat_id,))
            row = cursor.fetchone()
            if not row:
                raise KeyError(f"Seat {seat_id} not found")
            new_status = 0 if row["is_present"] == 1 else 1
            conn.execute("UPDATE seats SET is_present = ? WHERE id = ?", (new_status, seat_id))
            return {"id": seat_id, "is_present": bool(new_status)}

    def delete_seat(self, seat_id: str) -> bool:
        with self._connect(foreign_keys=False) as conn:
            conn.execute("DELETE FROM seats WHERE id = ?", (seat_id,))
        return True

    def load_preset_seats(self, preset_name: str, section_id: Optional[str] = None) -> List[Dict[str, Any]]:
        presets = {
            "2-seats": [
                {"id": "seat_1", "label": "Desk 1 (Left)", "student_name": "Empty (Desk 1)", "student_id_number": "", "x_min": 0.05, "y_min": 0.15, "x_max": 0.45, "y_max": 0.85, "is_present": True},
                {"id": "seat_2", "label": "Desk 2 (Right)", "student_name": "Empty (Desk 2)", "student_id_number": "", "x_min": 0.55, "y_min": 0.15, "x_max": 0.95, "y_max": 0.85, "is_present": True},
            ],
            "3-seats": [
                {"id": "seat_1", "label": "Desk 1 (Left)", "student_name": "Empty (Desk 1)", "student_id_number": "", "x_min": 0.03, "y_min": 0.15, "x_max": 0.32, "y_max": 0.85, "is_present": True},
                {"id": "seat_2", "label": "Desk 2 (Center)", "student_name": "Empty (Desk 2)", "student_id_number": "", "x_min": 0.35, "y_min": 0.15, "x_max": 0.65, "y_max": 0.85, "is_present": True},
                {"id": "seat_3", "label": "Desk 3 (Right)", "student_name": "Empty (Desk 3)", "student_id_number": "", "x_min": 0.68, "y_min": 0.15, "x_max": 0.97, "y_max": 0.85, "is_present": True},
            ],
            "4-seats": [
                {"id": "seat_1", "label": "Desk 1 (Front L)", "student_name": "Empty (Desk 1)", "student_id_number": "", "x_min": 0.05, "y_min": 0.45, "x_max": 0.45, "y_max": 0.95, "is_present": True},
                {"id": "seat_2", "label": "Desk 2 (Front R)", "student_name": "Empty (Desk 2)", "student_id_number": "", "x_min": 0.55, "y_min": 0.45, "x_max": 0.95, "y_max": 0.95, "is_present": True},
                {"id": "seat_3", "label": "Desk 3 (Back L)", "student_name": "Empty (Desk 3)", "student_id_number": "", "x_min": 0.05, "y_min": 0.05, "x_max": 0.45, "y_max": 0.42, "is_present": True},
                {"id": "seat_4", "label": "Desk 4 (Back R)", "student_name": "Empty (Desk 4)", "student_id_number": "", "x_min": 0.55, "y_min": 0.05, "x_max": 0.95, "y_max": 0.42, "is_present": True},
            ],
        }
        if preset_name not in presets:
            raise ValueError(f"Unknown preset: {preset_name}")

        target_sec = section_id or ""
        with self._connect(foreign_keys=False) as conn:
            if section_id:
                conn.execute("DELETE FROM seats WHERE section_id = ?;", (section_id,))
            else:
                conn.execute("DELETE FROM seats;")

        items = [
            {
                **s,
                "id": f"seat_{target_sec}_{s['id']}",
                "section_id": target_sec,
            }
            for s in presets[preset_name]
        ]
        return self.bulk_update_seats(items, section_id=target_sec)

    # -------------------------------------------------------------
    # RECITATION LEDGER (Grouped by Student + Number of Raises)
    # -------------------------------------------------------------
    def get_recitation_ledger(
        self,
        session_id: Optional[str] = None,
        section_id: Optional[str] = None,
        active_session_required: bool = False,
        active_podium_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        seats = self.get_seats(section_id=section_id)

        # If no session is started and active session is required, return idle 0 counts
        if active_session_required and not session_id:
            return [
                {
                    "seat_id": s["id"],
                    "label": s["label"],
                    "student_name": s["student_name"],
                    "student_id_number": s["student_id_number"],
                    "is_present": s["is_present"],
                    "total_raises": 0,
                    "total_points": 0,
                    "latest_event_id": None,
                    "latest_queue_pos": None,
                    "latest_delta_ms": 0,
                    "raised_at_ms": None,
                    "latest_status": "IDLE",
                    "latest_reason_code": None,
                    "latest_earned_point": None,
                }
                for s in seats
            ]

        with self._connect() as conn:
            # Optimized: Single SQL query for all matching events
            q = "SELECT * FROM events"
            p: List[Any] = []
            if session_id:
                q += " WHERE session_id = ?"
                p.append(session_id)
            q += " ORDER BY timestamp_ms DESC"
            cursor = conn.execute(q, p)
            all_events = [dict(r) for r in cursor.fetchall()]

        # Instant in-memory grouping
        events_by_seat: Dict[str, List[Dict[str, Any]]] = {}
        for evt in all_events:
            sid = evt.get("seat_id")
            if sid not in events_by_seat:
                events_by_seat[sid] = []
            events_by_seat[sid].append(evt)

        ledger = []
        for seat in seats:
            evts = events_by_seat.get(seat["id"], [])
            valid_raises = [e for e in evts if e["status"] == "VALID"]
            points = sum(1 for e in evts if e.get("earned_point") == 1)
            latest = evts[0] if evts else None

            # Determine live raise state (whether the student is raising their hand RIGHT NOW)
            if active_podium_map is not None:
                if seat["id"] in active_podium_map:
                    pod_info = active_podium_map[seat["id"]]
                    live_status = "VALID"
                    live_queue_pos = pod_info.get("queue_pos")
                    live_delta_ms = pod_info.get("delta_ms", 0)
                    raised_at_ms = pod_info.get("raised_at_ms")
                    live_reason = "VALID_HAND_RAISE"
                else:
                    live_status = "IDLE"
                    live_queue_pos = None
                    live_delta_ms = 0
                    raised_at_ms = None
                    live_reason = None
            else:
                if latest and latest["status"] == "VALID" and latest.get("reason_code") != "HAND_LOWERED":
                    live_status = "VALID"
                    live_queue_pos = latest.get("queue_pos")
                    live_delta_ms = latest.get("delta_ms", 0)
                    raised_at_ms = latest.get("timestamp_ms")
                    live_reason = latest.get("reason_code")
                else:
                    live_status = "IDLE"
                    live_queue_pos = None
                    live_delta_ms = 0
                    raised_at_ms = None
                    live_reason = None

            ledger.append({
                "seat_id": seat["id"],
                "label": seat["label"],
                "student_name": seat["student_name"],
                "student_id_number": seat["student_id_number"],
                "is_present": seat["is_present"],
                "photo_path": seat.get("photo_path", ""),
                "grid_row": seat.get("grid_row", 0),
                "grid_col": seat.get("grid_col", 0),
                "total_raises": len(valid_raises),
                "total_points": points,
                "latest_event_id": latest["id"] if latest else None,
                "latest_queue_pos": live_queue_pos,
                "latest_delta_ms": live_delta_ms,
                "raised_at_ms": raised_at_ms,
                "latest_status": live_status,
                "latest_reason_code": live_reason,
                "latest_earned_point": latest.get("earned_point") if latest else None,
                "verification_status": latest.get("verification_status", "VERIFIED") if latest else "VERIFIED",
                "face_similarity": float(latest.get("face_similarity", 1.0)) if latest else 1.0,
            })

        def sort_key(s):
            podium = s["latest_queue_pos"] if (s["latest_queue_pos"] and s["latest_status"] == "VALID") else 999
            return (podium, -s["total_raises"], s["label"])

        ledger.sort(key=sort_key)
        return ledger

    # -------------------------------------------------------------
    # EVENTS
    # -------------------------------------------------------------
    def insert_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events (
                    id, session_id, seat_id, student_name,
                    status, reason_code, arm_angle, duration_sec,
                    queue_pos, timestamp_ms, earned_point,
                    verification_status, face_similarity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event.get("session_id"),
                    event["seat_id"],
                    event["student_name"],
                    event["status"],
                    event["reason_code"],
                    float(event["arm_angle_deg"]),
                    float(event["duration_sec"]),
                    event.get("queue_pos"),
                    int(event["timestamp_ms"]),
                    event.get("earned_point"),
                    event.get("verification_status", "VERIFIED"),
                    float(event.get("face_similarity", 1.0)),
                ),
            )
        return event

    def award_point(self, event_id: str, force_override: bool = False) -> Dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            if not row:
                raise KeyError(f"Event {event_id} not found")
            if row["verification_status"] == "SEAT_MISMATCH" and not force_override:
                raise ValueError("Cannot award point: Student seat mismatch flagged (anti-proxy protection).")
            conn.execute("UPDATE events SET earned_point = 1 WHERE id = ?", (event_id,))
            return {"id": event_id, "status": "AWARDED", "earned_point": 1, "message": "+1 point awarded"}

    def dismiss_event(self, event_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            if not row:
                raise KeyError(f"Event {event_id} not found")
            conn.execute("UPDATE events SET earned_point = 0 WHERE id = ?", (event_id,))
            return {"id": event_id, "status": "DISMISSED", "earned_point": 0, "message": "Event marked false positive"}

    def get_events(self, session_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if session_id:
                cursor = conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp_ms DESC LIMIT ?",
                    (session_id, limit),
                )
            else:
                cursor = conn.execute("SELECT * FROM events ORDER BY timestamp_ms DESC LIMIT ?", (limit,))
            return [dict(r) for r in cursor.fetchall()]

    # -------------------------------------------------------------
    # EXPORTS
    # -------------------------------------------------------------
    def export_session_report_csv(self, session_id: Optional[str] = None, section_id: Optional[str] = None) -> str:
        with self._connect() as conn:
            query = """
            SELECT 
                s.id as seat_id,
                s.label as seat_label,
                s.student_name,
                s.student_id_number,
                CASE WHEN s.is_present = 1 THEN 'PRESENT' ELSE 'ABSENT' END as attendance_status,
                COALESCE(SUM(CASE WHEN e.earned_point = 1 THEN 1 ELSE 0 END), 0) as total_points_awarded,
                COALESCE(SUM(CASE WHEN e.status = 'VALID' THEN 1 ELSE 0 END), 0) as valid_raises,
                COALESCE(SUM(CASE WHEN e.status = 'INVALID' THEN 1 ELSE 0 END), 0) as invalid_gestures
            FROM seats s
            LEFT JOIN events e ON s.id = e.seat_id AND (? IS NULL OR e.session_id = ?)
            WHERE (? IS NULL OR s.section_id = ? OR s.section_id IS NULL)
            GROUP BY s.id, s.label, s.student_name, s.student_id_number, s.is_present ORDER BY s.label ASC
            """
            cursor = conn.execute(query, (session_id, session_id, section_id, section_id))
            rows = cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Seat ID",
            "Seat Label",
            "Student Name",
            "Student ID",
            "Attendance Status",
            "Total Points Awarded",
            "Valid Hand Raises",
            "Invalid Gestures",
        ])
        for r in rows:
            writer.writerow([
                r["seat_id"],
                r["seat_label"],
                r["student_name"],
                r["student_id_number"],
                r["attendance_status"],
                r["total_points_awarded"],
                r["valid_raises"],
                r["invalid_gestures"],
            ])

        return output.getvalue()

    def export_class_report_csv(self, section_id: Optional[str] = None) -> str:
        ledger = self.get_recitation_ledger(section_id=section_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Class Participation Report"])
        writer.writerow(["Generated At:", _now_iso()])
        writer.writerow([])
        writer.writerow([
            "Desk",
            "Student ID",
            "Student Name",
            "Attendance Status",
            "Total Hand Raises",
            "Recitation Points Awarded",
        ])
        for r in ledger:
            writer.writerow([
                r["label"],
                r["student_id_number"],
                r["student_name"],
                "PRESENT" if r["is_present"] else "ABSENT",
                r["total_raises"],
                r["total_points"],
            ])
        return output.getvalue()
