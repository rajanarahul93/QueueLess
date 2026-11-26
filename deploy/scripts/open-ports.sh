#!/usr/bin/env bash
# Lets TCP 80 and 443 through the host firewall ahead of the REJECT rule that Oracle's Ubuntu images ship in
# INPUT (it applies even when the VCN security list allows the port), saves the rules across reboots, and checks
# the result. Safe to repeat. Run as root; QL_DRY_RUN=1 prints each change instead of making it.
#
# ufw: provision-vm.sh also allows 22, 80 and 443 in ufw. ufw's chains sit behind Oracle's REJECT unless the
# ACCEPT rules below come first, so the two agree on the same ports and neither one blocks the other.
# QL_RULES_V4 is the persisted rules file (default /etc/iptables/rules.v4).
set -euo pipefail

PORTS="80 443"
RULES_V4=${QL_RULES_V4:-/etc/iptables/rules.v4}
dry=${QL_DRY_RUN:-0}
say() { printf '%s\n' "$*" >&2; }
run() { if [ "$dry" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
[ "$dry" = 1 ] || [ "$(id -u)" -eq 0 ] || { say "run as root (sudo)"; exit 1; }

# Position (1-based) in INPUT of the first REJECT or DROP rule, empty when there is none.
reject_pos() { iptables -S INPUT | awk 'NR > 1 { n++ } /^-A INPUT .*-j (REJECT|DROP)/ { print n; exit }'; }
# Position of the first whole-network ACCEPT rule for tcp port $1, empty when there is none.
accept_pos() {
  iptables -S INPUT | awk -v p="$1" 'NR > 1 { n++ } $0 !~ / -[sdi] / && $0 ~ ("^-A INPUT .*-p tcp .*--dport " p " -j ACCEPT") { print n; exit }'
}
# 0 when port $1 is accepted before any REJECT or DROP.
open_ahead() {
  local a r
  a=$(accept_pos "$1")
  r=$(reject_pos)
  [ -n "$a" ] && { [ -z "$r" ] || [ "$a" -lt "$r" ]; }
}
spec() { printf '%s\n' -p tcp -m state --state NEW -m tcp --dport "$1" -j ACCEPT; }

say "== iptables (allow TCP ${PORTS// /, } ahead of the default REJECT)"
if [ "$dry" = 1 ]; then
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables iptables-persistent
else
  echo 'iptables-persistent iptables-persistent/autosave_v4 boolean false' | debconf-set-selections
  echo 'iptables-persistent iptables-persistent/autosave_v6 boolean false' | debconf-set-selections
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables iptables-persistent
fi

for p in $PORTS; do
  if [ "$dry" = 1 ]; then
    run iptables -I INPUT "<position of the first REJECT or DROP rule>" "$(spec "$p" | tr '\n' ' ')"
  elif open_ahead "$p"; then
    say "port $p: already accepted ahead of the REJECT rule"
  else
    pos=$(reject_pos)
    # shellcheck disable=SC2046
    if [ -n "$pos" ]; then iptables -I INPUT "$pos" $(spec "$p"); else iptables -A INPUT $(spec "$p"); fi
    say "port $p: ACCEPT rule added"
  fi
done

say "== persist"
if [ ! -f "$RULES_V4" ]; then
  run netfilter-persistent save
else
  for p in $PORTS; do
    line="-A INPUT $(spec "$p" | tr '\n' ' ')"
    line=${line% }
    if grep -qxF -- "$line" "$RULES_V4"; then
      say "port $p: already in $RULES_V4"
    elif [ "$dry" = 1 ]; then
      printf '+ insert "%s" into %s ahead of its INPUT REJECT rule\n' "$line" "$RULES_V4"
    else
      tmp=$(mktemp)
      # Put the line before the first INPUT REJECT or DROP in *filter; if there is none, before that table's COMMIT.
      awk -v line="$line" '
        /^\*filter/ { f = 1 }
        f && !d && /^-A INPUT .*-j (REJECT|DROP)/ { print line; d = 1 }
        f && !d && /^COMMIT/ { print line; d = 1 }
        { print }' "$RULES_V4" >"$tmp"
      install -m 644 "$tmp" "$RULES_V4"
      rm -f "$tmp"
      say "port $p: saved in $RULES_V4"
    fi
  done
fi

if [ "$dry" = 1 ]; then
  say "dry run: nothing changed, nothing checked"
  exit 0
fi

say "== check"
bad=0
for p in $PORTS; do
  if open_ahead "$p"; then
    say "ok    port $p is accepted ahead of the REJECT rule"
  else
    say "FAIL  port $p is not accepted ahead of the REJECT rule in INPUT (iptables -S INPUT shows the order)"
    bad=1
  fi
  if [ -f "$RULES_V4" ] && ! grep -qE -- "--dport $p -j ACCEPT" "$RULES_V4"; then
    say "FAIL  port $p is not saved in $RULES_V4, so a reboot would close it"
    bad=1
  fi
done
[ "$bad" = 0 ] || exit 1
say "done"
