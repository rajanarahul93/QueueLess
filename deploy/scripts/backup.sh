#!/usr/bin/env bash
# pg_dump (custom format) -> gzip into $QL_BACKUP_DIR, checked with pg_restore --list, newest 7 kept.
# A side file holds each table's row count just before and just after the dump, for restore-drill.sh.
# Redis is not backed up: it holds only short-lived data (OTP codes, rate limits, idempotency keys, stream state).
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_env_file

KEEP=7
umask 077
mkdir -p "$BACKUP_DIR"
db=$(env_value POSTGRES_DB)
user=$(env_value POSTGRES_USER)
out="$BACKUP_DIR/queueless-$(date -u +%Y%m%dT%H%M%SZ).dump.gz"
work="$out.part"
trap 'rm -f "$work" "$work.before" "$work.after"' EXIT

table_counts "$db" >"$work.before"
compose exec -T postgres pg_dump -U "$user" -d "$db" -Fc --no-owner | gzip -c >"$work"
table_counts "$db" >"$work.after"

# A dump that cannot be listed is not a backup.
entries=$(gunzip -c "$work" | compose exec -T postgres pg_restore --list | grep -c '^[0-9]*; ' || true)
[ "$entries" -gt 10 ] || die "the dump lists only $entries entries; keeping nothing"

mv "$work" "$out"
join "$work.before" "$work.after" >"$out.counts"

# Retention: delete all but the newest $KEEP (names sort by time).
all=()
while IFS= read -r f; do all+=("$f"); done < <(list_backups)
if [ "${#all[@]}" -gt "$KEEP" ]; then
  for old in "${all[@]:0:${#all[@]}-KEEP}"; do rm -f "$old" "$old.counts"; done
fi
say "backup ok: $(basename "$out") ($(wc -c <"$out") bytes, $entries entries, $(wc -l <"$out.counts") tables)"
