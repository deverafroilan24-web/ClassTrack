# Manage ClassTrack administrators in Supabase

Once the migration and matching backend release are deployed, administrator setup
and password changes require no Python command or source-code changes.

## Create an administrator

Open your production Supabase project → **Table Editor → public → Admin → Insert row**.
Fill in:

| Column | Value |
| --- | --- |
| `id` | For the primary administrator, use `teacher_master` to retain legacy class ownership. For additional admins, leave the generated default. |
| `login_id` | Your chosen ID, for example `Admin1` (saved uppercase). |
| `name` | The administrator's display name. |
| `email` | The administrator's real email address. |
| `password` | Your chosen password: at least 12 characters, at most 72 UTF-8 bytes, at least four different characters. |
| Other columns | Leave their defaults. |

Save the row. The `password` field becomes NULL immediately; `password_hash` holds
the salted bcrypt hash. This is expected. Sign in at your dashboard's `/admin`
using **login_id and password**. Email is contact information, not the login ID.

## Change or disable access

- Reset a password by editing **password**, then save. Do not edit `password_hash`.
- Edit `login_id`, `name` or `email` in the same row.
- Set `is_active` to `0` to disable access, or `1` to enable it.
- Changes automatically increment `auth_version`, revoking previous sessions.
- Keep `id` unchanged; it preserves ownership of existing records.
- Use distinct IDs for teachers and administrators.

No default password or example account is created. Supabase's public `anon` and
`authenticated` API roles cannot read or modify this table. Use the Supabase project
dashboard with a project owner/admin account.

## Migration details

`migrations/20260926_supabase_admin.sql` adds the table and password trigger. It can
be reapplied without deleting data. It does not modify the running app's legacy
teacher accounts. The updated backend migrates existing password-based admin
accounts from `teachers` at startup, preserving hashes and ownership.

The trigger uses PostgreSQL's [pgcrypto password hashing functions](https://www.postgresql.org/docs/current/pgcrypto.html#PGCRYPTO-PASSWORD-HASHING-FUNCS).
