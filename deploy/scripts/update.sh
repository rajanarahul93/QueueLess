#!/usr/bin/env bash
# Pull, build, migrate, restart, wait for /readyz. If the new stack is not ready in time, put the previous
# api, worker and web images back and say so (exit 1).
# Usage: update.sh [--no-pull]
# Migrations are forward-only: a rollback restores the old code, not the old schema. Keep migrations additive.
# QL_READY_TIMEOUT (seconds, default 150) is how long the new stack gets.
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_env_file

timeout=${QL_READY_TIMEOUT:-150}
images=(queueless-backend queueless-web)

if [ "${1:-}" != "--no-pull" ]; then
  git -C "$DEPLOY_DIR/.." pull --ff-only
fi

# 1. Remember what runs now, but only when it is healthy: "previous" must always be a known-good set.
# When the stack is already down, the previous images from the last good update stay the rollback target.
have_previous=0
if ready_once; then
  for img in "${images[@]}"; do docker tag "$img:current" "$img:previous"; done
  have_previous=1
elif docker image inspect "${images[0]}:previous" "${images[1]}:previous" >/dev/null 2>&1; then
  say "warning: the running stack is not ready; keeping the previous images from the last good update"
  have_previous=1
else
  say "warning: the running stack is not ready and no previous images exist, so a rollback is not possible"
fi

# 2. Build. A failed build leaves the running stack untouched (set -e stops here).
compose build

# 3. Migrate with the new image, then roll out.
compose up -d --wait postgres redis
compose run --rm --no-deps api alembic upgrade head
# A dependency that never turns healthy makes "up" fail; the readiness check below decides what happens next.
compose up -d --remove-orphans || say "warning: compose up reported a failure"

# 4. Ready, or roll back.
if wait_ready "$timeout"; then
  say "update ok: the new stack is ready"
  exit 0
fi
say "the new stack is not ready after ${timeout}s"
if [ "$have_previous" -ne 1 ]; then
  die "nothing to roll back to; read: docker compose logs api worker web"
fi
for img in "${images[@]}"; do docker tag "$img:previous" "$img:current"; done
compose up -d --no-build --force-recreate api worker web || say "warning: compose up reported a failure"
if wait_ready "$timeout"; then
  die "ROLLED BACK to the previous images and they are ready; the update failed (read the api logs)"
fi
die "rolled back but the previous images are not ready either; investigate now"
