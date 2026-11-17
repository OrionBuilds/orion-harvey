#!/usr/bin/env bash
# One-time: put Harvey's Obsidian vault into Orion's Memory module, next to FRIDAY and CLARA.
#
#   ORION_REPO=/path/to/orion ORION_PROJECT_REF=<supabase-project-ref> bash scripts/enable_os_memory.sh
#
# ORION_REPO is an Orion checkout (linked with `supabase link`) on a branch that carries the
# harvey_memory migration; ORION_WORKTREE defaults to it.
#
# In the order that cannot break anything:
#   1. apply migration 20261103000000 (vault tables accept 'harvey'; HarveyAgent gets memory.sync)
#   2. deploy the vault-sync edge function (accepts the 'harvey' vault)
#   3. push the Orion branch to main (the Memory page gets its Harvey tab; your host deploys it)
#   4. turn on vault.os_memory in Harvey's config, restart it, and run the first sync
set -euo pipefail

REF="${ORION_PROJECT_REF:?set ORION_PROJECT_REF to your Supabase project ref}"
OS="${ORION_REPO:?set ORION_REPO to your Orion checkout}"
WT="${ORION_WORKTREE:-$OS}"
MIG="$WT/supabase/migrations/20261103000000_harvey_memory.sql"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

{ echo "BEGIN;"; cat "$MIG"; echo
  echo "INSERT INTO supabase_migrations.schema_migrations (version, name, statements) VALUES ('20261103000000','harvey_memory', ARRAY['applied via enable_os_memory.sh']) ON CONFLICT (version) DO NOTHING;"
  echo "COMMIT;"; } > "$WORK/apply.sql"

echo "1/4 Applying the migration..."
(cd "$OS" && supabase db query --linked -f "$WORK/apply.sql" >/dev/null)

echo "2/4 Deploying vault-sync..."
(cd "$WT" && supabase functions deploy vault-sync --project-ref "$REF" --yes)

echo "3/4 Pushing the Orion branch to main..."
(cd "$WT" && git fetch -q origin && git rebase origin/main && git push origin HEAD:main)

echo "4/4 Turning it on in Harvey and syncing..."
cd "${HARVEY_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("harvey.local.yaml"); s = p.read_text()
if "os_memory" not in s:
    s = re.sub(r"(vault:\n(?:  .*\n)*?)(\n|$)", lambda m: m.group(1) + "  os_memory: true\n" + m.group(2), s, count=1)
    p.write_text(s)
PY
source .venv/bin/activate
python -m harvey os sync >/dev/null
systemctl --user restart harvey 2>/dev/null || echo "Restart Harvey however you run it."
sleep 8
python - <<'PY'
import asyncio
from pathlib import Path
from harvey.config import load_config, load_env
from harvey.integrations.os_auth import OsAuth
from harvey.integrations.os_client import OsClient
from harvey.vault_sync import sync_to_os
async def main():
    cfg = load_config()
    print("Memory sync:", await sync_to_os(OsClient(OsAuth.from_env(load_env())), Path(cfg.vault.path)))
asyncio.run(main())
PY
echo "Done. Open Orion, Memory, Harvey tab."
