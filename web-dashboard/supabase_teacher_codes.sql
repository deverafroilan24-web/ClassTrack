-- Legacy PIN provisioning is retired. Do not insert plaintext credentials.
-- The backend migrates existing accounts to ID/password authentication,
-- preserving account IDs and class ownership. Run python manage_admin.py
-- with DATABASE_URL configured, then manage teacher accounts at /admin.
-- Accounts migrated from PINs need a new ID/password assigned by the admin.
ALTER TABLE public.teachers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.teachers FROM anon, authenticated;
