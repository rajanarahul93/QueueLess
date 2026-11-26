#!/usr/bin/env bash
# Restores the newest backup into a scratch database, compares the table list and row counts with the
# counts recorded around that dump, then drops the scratch database. Exit 0 only when everything matches.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_env_file

latest=$(list_backups | tail -n 1)
[ -n "$latest" ] || die "no backup in $BACKUP_DIR"
[ -r "$latest.counts" ] || die "$latest.counts is missing"
user=$(env_value POSTGRES_USER)
scratch="drill_$(date -u +%s)"

drop_scratch() { pg -d postgres -c "drop database if exists $scratch" >/dev/null 2>&1 || true; }
trap drop_scratch EXIT

pg -d postgres -c "create database $scratch" >/dev/null
gunzip -c "$latest" | compose exec -T postgres pg_restore -U "$user" -d "$scratch" --no-owner --exit-on-error
table_counts "$scratch" >"$latest.drill"
trap 'drop_scratch; rm -f "$latest.drill"' EXIT

failed=0
# Same tables on both sides.
if ! diff <(cut -d' ' -f1 "$latest.counts") <(cut -d' ' -f1 "$latest.drill") >/dev/null; then
  say "FAIL the restored tables differ from the tables that were dumped"
  failed=1
fi
# Each restored count lies between the count just before and just after the dump.
while read -r table before after; do
  got=$(sed -n "s/^$table //p" "$latest.drill")
  lo=$((before < after ? before : after))
  hi=$((before < after ? after : before))
  if [ -z "$got" ] || [ "$got" -lt "$lo" ] || [ "$got" -gt "$hi" ]; then
    say "FAIL $table: restored ${got:-none}, expected $lo to $hi"
    failed=1
  else
    say "ok   $table: $got rows"
  fi
done <"$latest.counts"
[ "$(pg -d "$scratch" -c 'select count(*) from alembic_version')" -ge 1 ] || { say "FAIL alembic_version is empty"; failed=1; }

[ "$failed" -eq 0 ] || die "restore drill failed for $(basename "$latest")"
say "restore drill ok: $(basename "$latest")"
