#!/usr/bin/env bash
# Writes the runtime env file with fresh random values, mode 600. Never prints them.
# Leaves an existing file alone, so running it again cannot change the database password under a live volume.
# Usage: gen-env.sh <hostname> [public-origin]      e.g. gen-env.sh 203-0-113-7.sslip.io
# Optional: QL_HTTP_PORT and QL_HTTPS_PORT set the host ports (a laptop test only; the VM keeps 80 and 443).
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

host=${1:?usage: gen-env.sh <hostname> [public-origin]}
origin=${2:-https://$host}
case $host in *[!A-Za-z0-9.-]* | "") die "the hostname may hold only letters, digits, dots and dashes" ;; esac
case $origin in https://*[!A-Za-z0-9.:-]* | https://) die "the origin must be https://<host>[:port]" ;; https://*) ;; *) die "the origin must start with https://" ;; esac

if [ -e "$ENV_FILE" ]; then
  say "$ENV_FILE exists; left as it is"
  exit 0
fi

umask 077
dir=$(dirname "$ENV_FILE")
mkdir -p "$dir"
tmp=$(mktemp "$dir/.env.XXXXXX")
trap 'rm -f "$tmp"' EXIT
r1=$(openssl rand -hex 32)
r2=$(openssl rand -hex 32)
r3=$(openssl rand -hex 32)
{
  printf 'SITE_HOST=%s\n' "$host"
  printf 'PUBLIC_ORIGIN=%s\n' "$origin"
  printf 'POSTGRES_USER=queueless\n'
  printf 'POSTGRES_DB=queueless\n'
  printf 'POSTGRES_PASSWORD=%s\n' "$r1"
  printf 'JWT_SECRET=%s\n' "$r2"
  printf 'STREAM_TOKEN_SECRET=%s\n' "$r3"
  printf 'LOG_LEVEL=INFO\n'
  [ -z "${QL_HTTP_PORT:-}" ] || printf 'HTTP_PORT=%s\n' "$QL_HTTP_PORT"
  [ -z "${QL_HTTPS_PORT:-}" ] || printf 'HTTPS_PORT=%s\n' "$QL_HTTPS_PORT"
} >"$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
trap - EXIT
say "wrote $ENV_FILE (mode 600); values not shown"
