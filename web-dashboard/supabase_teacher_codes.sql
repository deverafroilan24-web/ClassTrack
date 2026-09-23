-- Run once in Supabase Dashboard > SQL Editor.
-- The backend uses its private DATABASE_URL; public API roles cannot read this table.

CREATE TABLE IF NOT EXISTS public.teachers (
    id TEXT PRIMARY KEY DEFAULT ('teacher_' || replace(gen_random_uuid()::text, '-', '')),
    name TEXT NOT NULL,
    pin TEXT UNIQUE,
    department TEXT NOT NULL DEFAULT '',
    pin_hash TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.teachers ADD COLUMN IF NOT EXISTS pin TEXT;
ALTER TABLE public.teachers ALTER COLUMN pin_hash DROP NOT NULL;
ALTER TABLE public.teachers ALTER COLUMN id SET DEFAULT ('teacher_' || replace(gen_random_uuid()::text, '-', ''));
ALTER TABLE public.teachers ALTER COLUMN department SET DEFAULT '';
ALTER TABLE public.teachers ALTER COLUMN is_active SET DEFAULT 1;
ALTER TABLE public.teachers ALTER COLUMN auth_version SET DEFAULT 1;
ALTER TABLE public.teachers ALTER COLUMN created_at SET DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_pin_unique
    ON public.teachers(pin) WHERE pin IS NOT NULL;

ALTER TABLE public.teachers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.teachers FROM anon, authenticated;

-- Add each teacher with one row; the id/status/version/time use table defaults.
-- INSERT INTO public.teachers (name, pin, department)
-- VALUES ('Teacher Name', '5678', 'Department');
