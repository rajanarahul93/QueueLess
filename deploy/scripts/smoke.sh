#!/usr/bin/env bash
# HTTPS checks against a running stack, from outside it:
#   /healthz, /readyz, a request id, no public /docs, a demo login (the code is in the 202 body, the refresh
#   cookie is Secure), a public SSE stream that stays open and keeps sending heartbeats, and a rate-limit
#   check that proves a client-supplied X-Real-IP is ignored.
# Env: SMOKE_BASE      origin to test (default: PUBLIC_ORIGIN from the env file)
#      SMOKE_CACERT    CA file, for a local test with Caddy's internal CA
#      SMOKE_STREAM_SEC how long the stream must stay open (default 60)
#      SMOKE_SPOOF     1 (default) runs the rate-limit check last; it uses up this client's OTP budget for an hour
set -uo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
set +e

base=${SMOKE_BASE:-}
if [ -z "$base" ] && [ -r "$ENV_FILE" ]; then base=$(env_value PUBLIC_ORIGIN); fi
[ -n "$base" ] || die "set SMOKE_BASE (for example https://203-0-113-7.sslip.io)"
stream_sec=${SMOKE_STREAM_SEC:-60}
spoof=${SMOKE_SPOOF:-1}
command -v jq >/dev/null || die "jq is required"

curl_base=(-sS --max-time 20)
[ -z "${SMOKE_CACERT:-}" ] || curl_base+=(--cacert "$SMOKE_CACERT")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fails=0
pass() { printf 'ok    %s\n' "$1"; }
fail() { printf 'FAIL  %s: %s\n' "$1" "$2"; fails=$((fails + 1)); }

# http METHOD PATH [JSON] [extra curl args...] -> $code, $tmp/body, $tmp/head
http() {
  local method=$1 path=$2 data=${3:-}
  shift $(($# < 3 ? $# : 3))
  if [ -n "$data" ]; then
    code=$(curl "${curl_base[@]}" "$@" -X "$method" -H 'content-type: application/json' --data "$data" -o "$tmp/body" -D "$tmp/head" -w '%{http_code}' "$base$path")
  else
    code=$(curl "${curl_base[@]}" "$@" -X "$method" -o "$tmp/body" -D "$tmp/head" -w '%{http_code}' "$base$path")
  fi
}
new_phone() { printf '+919%09d' $((RANDOM * 32768 + RANDOM)); }

# --- liveness and readiness
http GET /healthz
if [ "$code" = 200 ] && [ "$(jq -r .status "$tmp/body")" = ok ]; then pass "/healthz is ok"; else fail "/healthz" "code $code: $(head -c 200 "$tmp/body")"; fi
http GET /readyz
if [ "$code" = 200 ] && [ "$(jq -r .status "$tmp/body")" = ok ]; then pass "/readyz is ok (postgres, redis, relay)"; else fail "/readyz" "code $code: $(head -c 200 "$tmp/body")"; fi
if grep -qi '^x-request-id: ' "$tmp/head"; then pass "a request id comes back"; else fail "x-request-id" "header missing"; fi

# --- the API's interactive docs are not routed to the public
http GET /openapi.json
if [ "$code" = 404 ]; then pass "/openapi.json is not public"; else fail "/openapi.json" "expected 404, got $code"; fi

# --- demo login: the code is shown, no SMS
phone=$(new_phone)
http POST /v1/auth/otp/request "{\"phone\": \"$phone\"}"
shown=$(jq -r '.demo_code // empty' "$tmp/body" 2>/dev/null)
if [ "$code" = 202 ] && [[ $shown =~ ^[0-9]{6}$ ]]; then
  pass "the OTP request shows a 6-digit demo code"
  http POST /v1/auth/otp/verify "{\"phone\": \"$phone\", \"code\": \"$shown\", \"name\": \"Smoke Test\"}"
  if [ "$code" = 200 ] && [ -n "$(jq -r '.access_token // empty' "$tmp/body")" ]; then
    pass "the demo code signs in"
    cookie=$(grep -i '^set-cookie: ' "$tmp/head" | tr -d '\r')
    if grep -qi 'secure' <<<"$cookie" && grep -qi 'httponly' <<<"$cookie" && grep -qi 'path=/v1/auth' <<<"$cookie"; then pass "the refresh cookie is Secure, HttpOnly, Path=/v1/auth"; else fail "refresh cookie" "got: $cookie"; fi
  else
    fail "demo sign-in" "code $code: $(head -c 200 "$tmp/body")"
  fi
else
  fail "demo OTP request" "code $code, demo_code '${shown:-none}': $(head -c 200 "$tmp/body")"
fi

# --- a public SSE stream stays open and keeps its heartbeat
http GET /v1/businesses
business=$(jq -r '.[0].id // empty' "$tmp/body" 2>/dev/null)
if [ -z "$business" ]; then
  email="smoke-$RANDOM$RANDOM@example.invalid"
  pw=$(openssl rand -hex 12)
  http POST /v1/auth/register "{\"name\": \"Smoke Owner\", \"email\": \"$email\", \"password\": \"$pw\"}"
  http POST /v1/auth/login "{\"email\": \"$email\", \"password\": \"$pw\"}"
  owner_bearer=$(jq -r '.access_token // empty' "$tmp/body")
  hours='{"mon":[{"open":"09:00","close":"21:00"}],"tue":[{"open":"09:00","close":"21:00"}],"wed":[{"open":"09:00","close":"21:00"}],"thu":[{"open":"09:00","close":"21:00"}],"fri":[{"open":"09:00","close":"21:00"}],"sat":[{"open":"09:00","close":"21:00"}],"sun":[{"open":"09:00","close":"21:00"}]}'
  http POST /v1/businesses "{\"name\": \"Smoke Test Clinic\", \"category\": \"clinic\", \"timezone\": \"Asia/Kolkata\", \"open_hours\": $hours}" -H "authorization: Bearer $owner_bearer"
  business=$(jq -r '.id // empty' "$tmp/body")
fi
if [ -z "$business" ]; then
  fail "stream set-up" "no business to watch (code $code)"
else
  curl "${curl_base[@]}" --max-time "$stream_sec" -N -H 'accept-encoding: gzip, zstd' -D "$tmp/shead" -o "$tmp/sbody" "$base/v1/businesses/$business/board"
  rc=$?
  beats=$(grep -c '^: heartbeat' "$tmp/sbody")
  need=$((stream_sec / 15 - 1))
  if [ "$rc" = 28 ]; then pass "the stream stayed open for ${stream_sec}s (curl timed out, did not end)"; else fail "stream stayed open" "curl exit $rc: the server or proxy ended it early"; fi
  if [ "$beats" -ge "$need" ]; then pass "$beats heartbeats in ${stream_sec}s (need $need)"; else fail "stream heartbeats" "$beats in ${stream_sec}s, need $need: the proxy is buffering"; fi
  if grep -qi '^content-type: text/event-stream' "$tmp/shead"; then pass "the stream is text/event-stream"; else fail "stream content type" "$(grep -i '^content-type' "$tmp/shead")"; fi
  if grep -qi '^content-encoding:' "$tmp/shead"; then fail "stream compression" "the stream was compressed: $(grep -i '^content-encoding' "$tmp/shead")"; else pass "the stream is not compressed"; fi
fi

# --- a client-supplied X-Real-IP must not change the rate-limit bucket (per-IP OTP limit: 20 an hour)
if [ "$spoof" = 1 ]; then
  limited=0
  for i in $(seq 1 30); do
    http POST /v1/auth/otp/request "{\"phone\": \"$(new_phone)\"}" -H "x-real-ip: 10.9.8.$i" -H "x-forwarded-for: 10.9.7.$i"
    if [ "$code" = 429 ]; then limited=$i; break; fi
  done
  if [ "$limited" -gt 0 ]; then
    pass "the per-IP limit tripped on request $limited although every request claimed a different X-Real-IP"
    http POST /v1/auth/otp/request "{\"phone\": \"$(new_phone)\"}" -H "x-real-ip: 198.51.100.77" -H "x-forwarded-for: 198.51.100.78"
    if [ "$code" = 429 ]; then pass "a new spoofed address shares the same bucket (still 429)"; else fail "spoofed address" "expected 429, got $code: the spoofed header selected a fresh bucket"; fi
  else
    fail "rate-limit spoof" "30 requests with 30 different spoofed X-Real-IP values never hit the per-IP limit: the header is trusted from the client"
  fi
else
  say "skipped  the rate-limit spoof check (SMOKE_SPOOF=0)"
fi

[ "$fails" -eq 0 ] && { say "smoke: all checks passed"; exit 0; }
say "smoke: $fails check(s) failed"
exit 1
