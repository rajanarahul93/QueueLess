#!/usr/bin/env bash
# The e2e sequence, exactly as CI runs it: Compose stack up (database first, migrated before
# the worker starts), then Playwright per browser. Each step is timed into a Markdown table
# ($GITHUB_STEP_SUMMARY in CI, stdout locally).
#   --fresh   remove the stack and its database volume first, so nothing depends on an
#             already-migrated local database
#   E2E_BROWSERS="chromium webkit"   limit the browsers (default: all three)
set -uo pipefail
cd "$(dirname "$0")/../.."

# The e2e-only override shortens live streams (20 s lifetime, 5 s heartbeat) so the tests see
# the server end a stream; local runs and production keep their defaults.
compose=(docker compose -f docker-compose.yml -f docker-compose.e2e.yml)
echo "Compose override active: docker-compose.e2e.yml (SSE_MAX_STREAM_SEC=20, SSE_HEARTBEAT_SEC=5)"

summary="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
failed=0
started=$SECONDS

timed() {
  local name=$1 began=$SECONDS status
  shift
  "$@"
  status=$?
  echo "| $name | $((SECONDS - began)) s | $([ "$status" -eq 0 ] && echo pass || echo FAIL) |" >>"$summary"
  [ "$status" -eq 0 ] || failed=1
  return "$status"
}

echo "| e2e step | time | result |" >>"$summary"
echo "| --- | --- | --- |" >>"$summary"
if [ "${1:-}" = "--fresh" ]; then
  timed "remove stack and database volume" "${compose[@]}" down -v || exit 1
fi
# The API's healthcheck is liveness only, so it is healthy before the tables exist; the
# worker's is not (its outbox relay needs them), so it starts after the migration.
timed "start postgres, redis, api" "${compose[@]}" up -d --build --wait postgres redis api || exit 1
timed "migrate" "${compose[@]}" run --rm api alembic upgrade head || exit 1
timed "start worker and web" "${compose[@]}" up -d --build --wait || exit 1
for browser in ${E2E_BROWSERS:-chromium firefox webkit}; do
  timed "e2e $browser" bash -c "cd web && npx playwright test --project=$browser"
done
echo "| total | $((SECONDS - started)) s | $([ "$failed" -eq 0 ] && echo pass || echo FAIL) |" >>"$summary"
exit "$failed"
