#!/usr/bin/env bash
# One-time (and safe to repeat) setup of a fresh Ubuntu arm64 VM, run as root:
#   sudo bash provision-vm.sh [--fail2ban]
# Installs Docker from Docker's own apt repository, a firewall for 22/80/443 (ufw plus the iptables rules from
# open-ports.sh, which Oracle's images need), automatic security updates, a 2 GB
# swap file, jq, a non-root "deploy" user (SSH keys copied from the invoking user), the nightly backup timer, and
# the directories the stack uses. QL_DRY_RUN=1 prints every change instead of making it.
set -euo pipefail

REPO_DIR=${QL_REPO_DIR:-/opt/queueless}
DEPLOY_USER=${QL_DEPLOY_USER:-deploy}
dry=${QL_DRY_RUN:-0}
say() { printf '%s\n' "$*" >&2; }
run() { if [ "$dry" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
[ "$dry" = 1 ] || [ "$(id -u)" -eq 0 ] || { say "run as root (sudo)"; exit 1; }

say "== packages"
run apt-get update -qq
run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg ufw jq git unattended-upgrades

say "== docker (official apt repository)"
if ! command -v docker >/dev/null 2>&1; then
  run install -m 0755 -d /etc/apt/keyrings
  run curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  run chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck source=/dev/null
  codename=$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")
  arch=$(dpkg --print-architecture)
  run sh -c "echo 'deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable' > /etc/apt/sources.list.d/docker.list"
  run apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  say "docker already installed"
fi
run systemctl enable --now docker

say "== firewall (22, 80, 443 in; the rest denied)"
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow 22/tcp
run ufw allow 80/tcp
run ufw allow 443/tcp
run ufw --force enable
# Oracle's Ubuntu images also reject inbound traffic in iptables itself; this puts 80 and 443 ahead of that rule.
bash "$(dirname "${BASH_SOURCE[0]}")/open-ports.sh" # honours QL_DRY_RUN itself

say "== automatic security updates"
run dpkg-reconfigure -f noninteractive unattended-upgrades

say "== swap file (2 GB)"
if ! swapon --show | grep -q '/swapfile'; then
  run fallocate -l 2G /swapfile
  run chmod 600 /swapfile
  run mkswap /swapfile
  run swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab 2>/dev/null || run sh -c "echo '/swapfile none swap sw 0 0' >> /etc/fstab"
else
  say "swap already on"
fi

if [ "${1:-}" = "--fail2ban" ]; then
  say "== fail2ban"
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban
  run systemctl enable --now fail2ban
fi

say "== deploy user"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  run adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
run usermod -aG docker "$DEPLOY_USER"
invoking=${SUDO_USER:-}
if [ -n "$invoking" ] && [ -r "/home/$invoking/.ssh/authorized_keys" ]; then
  run install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
  run install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$invoking/.ssh/authorized_keys" "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi

say "== directories and the nightly backup timer"
run install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /etc/queueless /var/backups/queueless
run install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$REPO_DIR"
if [ -d "$REPO_DIR/deploy/systemd" ]; then
  run install -m 644 "$REPO_DIR/deploy/systemd/queueless-backup.service" /etc/systemd/system/queueless-backup.service
  run install -m 644 "$REPO_DIR/deploy/systemd/queueless-backup.timer" /etc/systemd/system/queueless-backup.timer
  run systemctl daemon-reload
  run systemctl enable --now queueless-backup.timer
else
  say "clone the repository to $REPO_DIR first, then run this script again to install the backup timer"
fi
say "done"
