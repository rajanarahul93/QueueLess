#!/usr/bin/env bash
# Sets the demo up on a fresh Ubuntu VM from your own machine, over SSH. Safe to run again.
# Usage: bootstrap-remote.sh [--dry-run] [--site HOSTNAME] <vm-ip-or-hostname>
#   --dry-run     print every command instead of running it
#   --site NAME   the public hostname for HTTPS (default: the IPv4 address as dashes + .sslip.io, or the argument itself)
# Env: QL_SSH_KEY (default ~/.ssh/queueless_oracle), QL_SSH_USER (default ubuntu), QL_SSH_PORT (default 22),
#      QL_REPO_URL (default the public GitHub repository), QL_SSH_WAIT seconds to wait for SSH (default 300),
#      QL_SMOKE_SEC seconds the stream check stays open (default 60).
# The key file is named, never its path. The env file with the generated values stays on the VM.
# shellcheck disable=SC2029,SC2329 # remote commands expand here on purpose; step functions are called through step()
set -euo pipefail

dry=0
site=""
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) dry=1 ;;
    --site) site=${2:?--site needs a hostname}; shift ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) break ;;
  esac
  shift
done
target=${1:?usage: bootstrap-remote.sh [--dry-run] [--site HOSTNAME] <vm-ip-or-hostname>}
[ $# -eq 1 ] || { echo "one target only" >&2; exit 2; }

key=${QL_SSH_KEY:-$HOME/.ssh/queueless_oracle}
user=${QL_SSH_USER:-ubuntu}
port=${QL_SSH_PORT:-22}
repo=${QL_REPO_URL:-https://github.com/rajanarahul93/QueueLess.git}
wait_s=${QL_SSH_WAIT:-300}
smoke_sec=${QL_SMOKE_SEC:-60}
dir=/opt/queueless
envf=/etc/queueless/prod.env

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $*"; exit 1; }
valid_name() { case $1 in *[!A-Za-z0-9.-]* | "") return 1 ;; esac; }

valid_name "$target" || die "the target may hold only letters, digits, dots and dashes"
if [ -z "$site" ]; then
  case $target in
    *[!0-9.]*) site=$target ;;
    *) site="$(printf '%s' "$target" | tr . -).sslip.io" ;;
  esac
fi
valid_name "$site" || die "the site hostname may hold only letters, digits, dots and dashes"
[ "$dry" = 1 ] || [ -r "$key" ] || die "SSH key $(basename "$key") not found"

ssh_opts=(-i "$key" -p "$port" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes
  -o ConnectTimeout=10 -o ServerAliveInterval=30)
dest="$user@$target"

# Prints the command with the key shown by name only.
show() { printf '+ %s\n' "${*//"$key"/<key:$(basename "$key")>}"; }
# rssh CMD: the remote command, run through a login shell on the VM.
rssh() {
  if [ "$dry" = 1 ]; then show ssh "${ssh_opts[@]}" "$dest" "$1"; else ssh "${ssh_opts[@]}" "$dest" "$1"; fi
}

summary=()
step() {
  local name=$1
  shift
  say "== $name"
  if "$@"; then summary+=("ok    $name"); else summary+=("FAIL  $name"); finish 1; fi
}
finish() {
  say ""
  say "summary for $site (VM $target)"
  printf '%s\n' "${summary[@]}" >&2
  if [ "$1" != 0 ]; then say "result: FAILED, fix the step above and run this script again"
  elif [ "$dry" = 1 ]; then say "result: dry run, nothing was run"
  else say "result: success"; fi
  exit "$1"
}

wait_for_ssh() {
  local waited=0
  if [ "$dry" = 1 ]; then show ssh "${ssh_opts[@]}" "$dest" true "# repeated every 5 s for up to ${wait_s} s"; return 0; fi
  until ssh "${ssh_opts[@]}" "$dest" true 2>/dev/null; do
    [ "$waited" -lt "$wait_s" ] || { say "no SSH answer from $target after ${wait_s}s"; return 1; }
    sleep 5
    waited=$((waited + 5))
  done
}

fetch_repo() {
  # After provisioning the checkout belongs to "deploy", so pull as whoever owns it.
  rssh "command -v git >/dev/null || sudo apt-get install -y -qq git; \
if [ -d $dir/.git ]; then sudo -u \"\$(stat -c %U $dir/.git)\" -H git -C $dir pull --ff-only; \
else sudo install -d -o $user -g $user $dir && git clone $repo $dir; fi"
}

provision() {
  rssh "sudo bash $dir/deploy/scripts/provision-vm.sh && sudo chown -R deploy:deploy $dir"
}

# gen-env.sh leaves an existing file alone, so a changed hostname is written into it here (names, not secrets).
write_env() {
  rssh "sudo -u deploy -H $dir/deploy/scripts/gen-env.sh $site && sudo -u deploy -H sed -i \
-e 's|^SITE_HOST=.*|SITE_HOST=$site|' -e 's|^PUBLIC_ORIGIN=.*|PUBLIC_ORIGIN=https://$site|' $envf \
&& test \"\$(sudo stat -c %a $envf)\" = 600 && echo 'env file: mode 600, values not shown'"
}

update() { rssh "sudo -u deploy -H $dir/deploy/scripts/update.sh --no-pull"; }

smoke_on_vm() { rssh "sudo -u deploy -H env SMOKE_STREAM_SEC=$smoke_sec SMOKE_BASE=https://$site $dir/deploy/scripts/smoke.sh"; }

# From this machine, to the public name. Needs curl and jq here.
smoke_here() {
  local script
  script=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke.sh
  if [ "$dry" = 1 ]; then
    printf '+ env SMOKE_STREAM_SEC=%s SMOKE_BASE=https://%s %s\n' "$smoke_sec" "$site" "$script"
  else
    SMOKE_STREAM_SEC=$smoke_sec SMOKE_BASE=https://$site "$script"
  fi
}

say "VM $target, site $site, ssh as $user with key $(basename "$key")"
step "wait for SSH" wait_for_ssh
step "fetch the repository" fetch_repo
step "provision the VM" provision
step "write the env file" write_env
step "build and start (update.sh)" update
step "smoke test from the VM" smoke_on_vm
step "smoke test from here" smoke_here
finish 0
