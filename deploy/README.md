# Deploying the demo on one free VM

One Oracle Cloud Always Free Ampere A1 VM (arm64) runs the whole stack with Docker Compose: Caddy (the only
published ports, 80 and 443), the web app, the API, the worker, Postgres 16 and Redis 7. The web app and the API
share one origin, so the refresh cookie stays host-only and no CORS is involved.

Nothing in this folder holds a real hostname, address or secret. Every value is generated or typed on the VM.

| File | What it is |
|---|---|
| `compose.prod.yml` | The production stack. A standalone file, not an overlay on `../docker-compose.yml`. |
| `Caddyfile` | One hostname, automatic HTTPS, path routing, SSE settings, client-IP header. |
| `scripts/bootstrap-remote.sh` | Runs everything below from your own machine over SSH (the one-command path). |
| `scripts/provision-vm.sh` | First-time VM setup (Docker, firewall, updates, swap, deploy user, backup timer). |
| `scripts/open-ports.sh` | Opens TCP 80 and 443 in iptables ahead of Oracle's default REJECT rule and saves them. |
| `scripts/gen-env.sh` | Writes the runtime env file with random values, mode 600, never printing them. |
| `scripts/update.sh` | Pull, build, migrate, restart, wait for `/readyz`, roll back on failure. |
| `scripts/backup.sh`, `scripts/restore-drill.sh` | Nightly dump (7 kept) and a restore check. |
| `scripts/smoke.sh` | HTTPS checks from outside, including a 60 s live stream and a spoofed-header test. |
| `systemd/` | The nightly backup timer. |

## What only you can do (once)

1. **Oracle account.** Sign up at oracle.com/cloud/free with a card (used for identity; Always Free resources are not
   charged). Pick a home region and keep in mind that Always Free resources exist only there.
2. **Create the VM.** Compute, Instances, Create instance:
   - Image: Canonical Ubuntu (22.04 or 24.04), shape VM.Standard.A1.Flex, 1 OCPU, **6 GB memory** (the stack uses about
     1.3 GB; 6 GB keeps memory use above Oracle's idle threshold of 20% on average, see "Idle reclaim" below).
   - Networking: a public subnet, **assign a public IPv4 address**.
   - Add your SSH public key (paste the line, not the private file).
   - "Out of host capacity" is temporary: try again later or another availability domain.
3. **Open ports 80 and 443** in the VCN: Networking, Virtual Cloud Networks, your VCN, Security Lists, Add Ingress
   Rules: source `0.0.0.0/0`, TCP, destination port 80; repeat for 443. Port 22 is open already.
4. **Choose a free hostname.** Two options, both free:
   - **DuckDNS (recommended for the real demo).** Create a name at duckdns.org (sign-in with an existing account) and
     point it at the VM's public IP. `duckdns.org` is on the Public Suffix List, so Let's Encrypt counts every DuckDNS
     name as its own registered domain and your certificate limit is yours alone.
   - **sslip.io (quick test, no sign-up).** Write the IP with dashes plus `.sslip.io`: `203.0.113.7` becomes
     `203-0-113-7.sslip.io`. `sslip.io` is **not** on the Public Suffix List, so all of its users share one
     Let's Encrypt limit of 50 new certificates per 7 days for the registered domain `sslip.io`. When that pool is used
     up, issuance for a new name fails until it refills (one certificate per 202 minutes). Caddy then tries ZeroSSL on
     its own, which may still work. It is a volunteer service without an SLA.

   Either way, never delete the `caddy_data` volume (`down -v`): it holds the certificate, and re-issuing for the same
   name is limited to 5 per week.

## One command from your machine

With the VM running and its public IP known (and the DuckDNS name pointing at it, if you use one):

```bash
deploy/scripts/bootstrap-remote.sh 203.0.113.7                            # site name 203-0-113-7.sslip.io
deploy/scripts/bootstrap-remote.sh --site my-demo.duckdns.org 203.0.113.7 # site name chosen by you
deploy/scripts/bootstrap-remote.sh --dry-run 203.0.113.7                  # print every command, run nothing
```

It uses the key `~/.ssh/queueless_oracle` (`QL_SSH_KEY` to change it) and the `ubuntu` user (`QL_SSH_USER`). Steps: wait
for SSH, clone this repository over https to `/opt/queueless`, run `provision-vm.sh`, run `gen-env.sh` and set the site
name (the env file stays on the VM, mode 600), run `update.sh`, run `smoke.sh` on the VM and then from your machine
(needs `curl` and `jq` here), and print a summary. It stops at the first failed step and can be run again. The smoke
test takes about 70 seconds per run. The manual steps below do the same thing one at a time.

## First deploy (manual)

On the VM, as the default `ubuntu` user:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo install -d -o ubuntu -g ubuntu /opt/queueless
git clone https://github.com/rajanarahul93/QueueLess.git /opt/queueless
sudo bash /opt/queueless/deploy/scripts/provision-vm.sh
```

Then as the `deploy` user (log out and back in over SSH as `deploy`):

```bash
cd /opt/queueless
deploy/scripts/gen-env.sh 203-0-113-7.sslip.io    # your hostname; writes /etc/queueless/prod.env, values not shown
deploy/scripts/update.sh --no-pull                 # builds (several minutes on 1 OCPU), migrates, starts, waits for /readyz
deploy/scripts/smoke.sh                            # takes about 70 seconds
```

`gen-env.sh` leaves an existing env file alone, so running it again never changes the database password.
Caddy fetches the certificate on the first request to port 80 or 443; check `docker compose -f deploy/compose.prod.yml
--env-file /etc/queueless/prod.env logs caddy` if the first HTTPS request fails.

## Update

```bash
cd /opt/queueless && deploy/scripts/update.sh
```

It pulls, builds, runs `alembic upgrade head`, restarts, and waits up to 150 s for `/readyz`. If the new stack is not
ready, it puts the previous images back and exits non-zero. Migrations are forward-only: a rollback restores the old
code, not the old schema, so keep migrations additive.

## Backup and restore

The timer installed by `provision-vm.sh` runs `backup.sh` nightly (03:15 plus up to 10 minutes). Dumps are in
`/var/backups/queueless` (7 kept, each checked with `pg_restore --list`). To pull them to your own machine:

```bash
scp deploy@203-0-113-7.sslip.io:/var/backups/queueless/queueless-*.dump.gz ./
```

Prove the backups restore (do this once now, then now and then):

```bash
cd /opt/queueless && deploy/scripts/backup.sh && deploy/scripts/restore-drill.sh
```

The drill restores the newest dump into a scratch database, compares table names and row counts with the counts
recorded around the dump, and drops the scratch database.

To restore for real after losing the data volume: bring up only Postgres (`docker compose ... up -d postgres`), then
`gunzip -c queueless-<time>.dump.gz | docker compose ... exec -T postgres pg_restore -U queueless -d queueless --clean --if-exists --no-owner`,
then run `update.sh --no-pull`. Redis holds only short-lived data and needs no backup.

## Rollback by hand

`update.sh` rolls back by itself. To go back later: `git checkout <older commit>` in `/opt/queueless` and run
`deploy/scripts/update.sh --no-pull`.

## Teardown

```bash
cd /opt/queueless
docker compose --env-file /etc/queueless/prod.env -f deploy/compose.prod.yml down      # stop, keep the data
docker compose --env-file /etc/queueless/prod.env -f deploy/compose.prod.yml down -v   # stop and DELETE the data
```

To remove everything, terminate the instance in the Oracle console.

## Things to know

- **Idle reclaim.** Oracle may reclaim Always Free instances whose CPU, network and (on A1) memory use all stay under 20%
  for 7 days. A quiet demo meets the CPU and network tests; 6 GB of memory is there to keep memory above the line. Watch
  the instance's metrics in the console.
- **Two firewalls.** Oracle's Ubuntu images reject inbound traffic in iptables except SSH, on top of the VCN security
  list, so opening the VCN ports is not enough. `provision-vm.sh` runs `open-ports.sh`, which puts ACCEPT rules for TCP
  80 and 443 ahead of that REJECT rule, saves them in `/etc/iptables/rules.v4`, and fails loudly if they are not in
  place. ufw allows the same ports, so the two agree. To check by hand: `sudo iptables -S INPUT` must list the two
  ACCEPT rules above the REJECT line. Docker's published ports bypass these INPUT rules (and ufw), so what is really
  exposed is the port list in `compose.prod.yml`: only Caddy publishes any.
- **Demo mode.** `APP_ENV=demo` and `OTP_PROVIDER=demo` show the sign-in code on screen and send no SMS. Do not point
  this at real customers. The site banner says data may be reset.
- **Secrets.** `/etc/queueless/prod.env` (mode 600) is the only place they live. It is not in the repository, not in
  any image layer, and not in a build argument. Treat a printout of `docker compose config` as sensitive: it contains them.
- **Public client address.** Caddy sets `X-Real-IP` from the connection on every request and the API trusts only that
  header. If you put another proxy or CDN in front of Caddy, this stops being true; change the setup first.
