-- Run once in Supabase SQL Editor, or apply through the backend database connection.
-- Additive: does not change teachers or invalidate the currently deployed PIN login.
CREATE TABLE IF NOT EXISTS public."Admin" (
    id TEXT PRIMARY KEY DEFAULT ('admin_' || gen_random_uuid()::text),
    login_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT 'Administrator',
    email TEXT UNIQUE,
    password_hash TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    password TEXT
);
ALTER TABLE public."Admin" ADD COLUMN IF NOT EXISTS password TEXT;
ALTER TABLE public."Admin" ALTER COLUMN id SET DEFAULT ('admin_' || gen_random_uuid()::text);
ALTER TABLE public."Admin" ALTER COLUMN name SET DEFAULT 'Administrator';
ALTER TABLE public."Admin" ALTER COLUMN created_at SET DEFAULT (CURRENT_TIMESTAMP::text);

-- pgcrypto is installed in Supabase's extensions schema.
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;

CREATE OR REPLACE FUNCTION public.classtrack_admin_credentials()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    NEW.login_id := upper(btrim(NEW.login_id));
    NEW.email := nullif(lower(btrim(NEW.email)), '');
    NEW.name := btrim(NEW.name);
    IF NEW.login_id IS NULL OR NEW.login_id !~ '^[A-Z0-9._/-]{1,64}$' THEN
        RAISE EXCEPTION 'Admin ID must contain 1-64 letters, digits, dots, hyphens, slashes or underscores';
    END IF;
    IF NEW.name IS NULL OR length(NEW.name) NOT BETWEEN 1 AND 120 THEN
        RAISE EXCEPTION 'Admin name must contain 1-120 characters';
    END IF;
    IF NEW.email IS NOT NULL AND (length(NEW.email) > 254 OR NEW.email !~ '^[^[:space:]@<>]+@[^[:space:]@<>]+\.[^[:space:]@<>]+$') THEN
        RAISE EXCEPTION 'Enter a valid admin email address';
    END IF;
    IF NEW.is_active NOT IN (0, 1) THEN
        RAISE EXCEPTION 'is_active must be 0 or 1';
    END IF;
    IF NEW.password IS NOT NULL THEN
        IF NEW.email IS NULL THEN
            RAISE EXCEPTION 'Email is required when setting an admin password';
        END IF;
        -- bcrypt consumes at most 72 bytes. Reject longer input, never truncate it.
        IF length(NEW.password) < 12 OR octet_length(NEW.password) > 72 OR
           (SELECT count(DISTINCT ch) FROM regexp_split_to_table(NEW.password, '') AS ch) < 4 THEN
            RAISE EXCEPTION 'Use a varied password with at least 12 characters and at most 72 UTF-8 bytes';
        END IF;
        NEW.password_hash := extensions.crypt(NEW.password, extensions.gen_salt('bf', 12));
        NEW.password := NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'Admin internal ID cannot be changed';
        END IF;
        NEW.auth_version := OLD.auth_version + 1;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS classtrack_admin_credentials ON public."Admin";
CREATE TRIGGER classtrack_admin_credentials
BEFORE INSERT OR UPDATE ON public."Admin"
FOR EACH ROW EXECUTE FUNCTION public.classtrack_admin_credentials();

ALTER TABLE public."Admin" ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public."Admin" FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.classtrack_admin_credentials() FROM PUBLIC, anon, authenticated;

COMMENT ON COLUMN public."Admin".password IS
    'Write-only password input: enter a new password here; the trigger hashes it and saves NULL.';
COMMENT ON COLUMN public."Admin".password_hash IS
    'Managed automatically. Change password through the password column.';
COMMENT ON COLUMN public."Admin".auth_version IS
    'Managed automatically. Changes revoke existing administrator sessions.';
