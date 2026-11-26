# Shared by the deploy scripts. Source it; do not run it.
# shellcheck shell=bash
set -euo pipefail

DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE=${QL_ENV_FILE:-/etc/queueless/prod.env}
BACKUP_DIR=${QL_BACKUP_DIR:-/var/backups/queueless}

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }

need_env_file() { [ -r "$ENV_FILE" ] || die "env file $ENV_FILE is missing or unreadable (run gen-env.sh first)"; }

compose() { docker compose --env-file "$ENV_FILE" -f "$DEPLOY_DIR/compose.prod.yml" "$@"; }

# One value from the env file, without sourcing it. Callers pass names that hold no secret.
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1; }

# psql inside the postgres container over its local socket: nothing sensitive in argv or the environment.
pg() { compose exec -T postgres psql -U "$(env_value POSTGRES_USER)" -v ON_ERROR_STOP=1 -X -A -t "$@"; }

# "table count" lines, sorted, for every table in the public schema of database $1.
table_counts() {
  local sql
  sql=$(pg -d "$1" -c "select string_agg(format('select %L as t, count(*) as n from %I.%I', table_name, table_schema, table_name), ' union all ' order by table_name) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'")
  [ -n "$sql" ] || die "no tables in $1"
  pg -d "$1" -c "select t || ' ' || n from ($sql) q order by t" | LC_ALL=C sort
}

# The API answers /readyz through the same network Caddy uses. Non-zero while it says 503.
ready_once() { compose exec -T caddy wget -q -O - http://api:8000/readyz >/dev/null 2>&1; }

# wait_ready [seconds]: poll /readyz every 3 s.
wait_ready() {
  local limit=${1:-120} waited=0
  while [ "$waited" -lt "$limit" ]; do
    ready_once && return 0
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# Backups newest last. Empty output when there are none.
list_backups() {
  local f
  for f in "$BACKUP_DIR"/queueless-*.dump.gz; do [ -e "$f" ] && printf '%s\n' "$f"; done | LC_ALL=C sort
}
