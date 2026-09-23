import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL")
print(f"Connecting to Supabase at: {DB_URL.split('@')[-1] if DB_URL else 'None'}...")

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    section_id TEXT REFERENCES sections(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    student_id_number TEXT NOT NULL DEFAULT '',
    photo_path TEXT,
    face_embedding TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    section_id TEXT REFERENCES sections(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS seats (
    id TEXT PRIMARY KEY,
    section_id TEXT REFERENCES sections(id) ON DELETE CASCADE,
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
    timestamp_ms BIGINT NOT NULL,
    earned_point INTEGER,
    verification_status TEXT NOT NULL DEFAULT 'VERIFIED',
    face_similarity REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS teachers (
    id TEXT PRIMARY KEY DEFAULT ('teacher_' || replace(gen_random_uuid()::text, '-', '')),
    name TEXT NOT NULL,
    pin TEXT UNIQUE,
    department TEXT NOT NULL DEFAULT '',
    pin_hash TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE teachers ADD COLUMN IF NOT EXISTS pin TEXT;
ALTER TABLE teachers ALTER COLUMN pin_hash DROP NOT NULL;
ALTER TABLE teachers ALTER COLUMN id SET DEFAULT ('teacher_' || replace(gen_random_uuid()::text, '-', ''));
ALTER TABLE teachers ALTER COLUMN department SET DEFAULT '';
ALTER TABLE teachers ALTER COLUMN is_active SET DEFAULT 1;
ALTER TABLE teachers ALTER COLUMN auth_version SET DEFAULT 1;
ALTER TABLE teachers ALTER COLUMN created_at SET DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_seat ON events (seat_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_seats_section ON seats (section_id);
CREATE INDEX IF NOT EXISTS idx_students_section ON students (section_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_pin_unique ON teachers(pin) WHERE pin IS NOT NULL;
"""

try:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        print("✅ Supabase schema executed successfully!")
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public';")
        tables = [t[0] for t in cur.fetchall()]
        print("Existing public tables in Supabase:", tables)
    conn.close()
except Exception as e:
    print(f"❌ Error connecting or initializing schema: {e}")
