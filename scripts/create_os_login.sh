#!/usr/bin/env bash
# One-time: create Harvey's Orion login, give it the HarveyAgent role, and sign Harvey in.
#
# Run it yourself in a terminal:
#   HARVEY_ORION_EMAIL=harvey@yourcompany.com ORION_REPO=/path/to/orion bash scripts/create_os_login.sh
#
# What it does (nothing here is printed or stored except Harvey's rotating refresh token):
#   1. generates a random password in memory / a 0600 temp file
#   2. inserts the auth user $HARVEY_ORION_EMAIL (no mailbox needed; it is just an identifier)
#      into the Orion database linked in $ORION_REPO, through your logged-in `supabase` CLI
#   3. runs harvey_provision(): HarveyAgent role + pinned thread in Conversations
#   4. signs Harvey in once with that password and keeps only the refresh token
#      (~/.agentic-os/harvey/os-session.json, mode 600), then deletes the password
set -euo pipefail

EMAIL="${HARVEY_ORION_EMAIL:?set HARVEY_ORION_EMAIL to the login to create, e.g. harvey@yourcompany.com}"
OS_REPO="${ORION_REPO:?set ORION_REPO to your Orion checkout (linked with supabase link)}"
HARVEY="${HARVEY_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"

umask 077
WORK="$(mktemp -d)"
trap 'shred -u "$WORK"/* 2>/dev/null || true; rm -rf "$WORK"' EXIT

cd "$OS_REPO"
existing="$(supabase db query --linked "select count(*) as n from auth.users where email='$EMAIL'" 2>/dev/null | grep -o '"n": *[0-9]*' | grep -o '[0-9]*$' || echo 0)"
if [ "${existing:-0}" != "0" ]; then
  echo "A login for $EMAIL already exists. Delete it in the Supabase dashboard first if you want a fresh one." >&2
  exit 1
fi

source "$HARVEY/.venv/bin/activate"
python - "$WORK" "$EMAIL" <<'PY'
import secrets, sys, uuid
work, email = sys.argv[1], sys.argv[2]
pw, uid = secrets.token_urlsafe(32), str(uuid.uuid4())
open(f"{work}/pw", "w").write(pw)
open(f"{work}/create.sql", "w").write(f"""BEGIN;
INSERT INTO auth.users (instance_id,id,aud,role,email,encrypted_password,email_confirmed_at,
  raw_app_meta_data,raw_user_meta_data,created_at,updated_at,confirmation_token,recovery_token,email_change_token_new,email_change)
VALUES ('00000000-0000-0000-0000-000000000000','{uid}','authenticated','authenticated','{email}',
  extensions.crypt('{pw}', extensions.gen_salt('bf')), now(),
  '{{"provider":"email","providers":["email"]}}','{{"full_name":"Harvey"}}', now(), now(), '', '', '', '');
INSERT INTO auth.identities (id,user_id,provider_id,provider,identity_data,last_sign_in_at,created_at,updated_at)
VALUES (gen_random_uuid(),'{uid}','{uid}','email',
  jsonb_build_object('sub','{uid}','email','{email}','email_verified',true,'phone_verified',false), now(), now(), now());
COMMIT;
""")
PY

echo "Creating the login..."
supabase db query --linked -f "$WORK/create.sql" >/dev/null
echo "Giving it the HarveyAgent role and pinning its thread..."
supabase db query --linked "select harvey_provision()" | grep -E '"role"|error' || true

echo "Signing Harvey in..."
cd "$HARVEY"
python - "$WORK/pw" "$EMAIL" <<'PY'
import sys
from harvey.config import load_env
from harvey.integrations.os_auth import OsAuth
pw = open(sys.argv[1]).read()
auth = OsAuth.from_env(load_env())
auth.login_password(sys.argv[2], pw)
print(f"Signed in as {auth.email()}. Only the refresh token was kept.")
PY

systemctl --user restart harvey 2>/dev/null || echo "Restart Harvey however you run it."
sleep 3
python -m harvey os status
echo "Done. The Harvey page and its thread should appear in Orion within a minute."
