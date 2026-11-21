import { execFileSync } from "node:child_process";
import { join } from "node:path";

// Test-only helpers that talk to the local Compose stack. Nothing here is imported by the app.
const ROOT = join(__dirname, "..", "..");
export const API_URL = process.env.API_URL ?? "http://localhost:8000";

function compose(...args: string[]): string {
  return execFileSync("docker", ["compose", ...args], { cwd: ROOT, encoding: "utf8" });
}

/** Clears the rate-limit buckets (rl:*) in the stack's Redis so repeated e2e runs stay within
 *  the login limits. Refuses unless the API runs with APP_ENV=local. */
function requireLocal(): void {
  const env = compose("exec", "-T", "api", "printenv", "APP_ENV").trim();
  if (env !== "local") throw new Error(`refusing to touch the stack: APP_ENV is ${env || "unset"}`);
}

export function clearRateLimits(): void {
  requireLocal();
  compose(
    "exec",
    "-T",
    "redis",
    "sh",
    "-c",
    "redis-cli --scan --pattern 'rl:*' | xargs -r redis-cli del",
  );
}

/** Gives the customer with this phone three no-shows, so booking is restricted (local only). */
export function restrictBooking(phone: string): void {
  requireLocal();
  if (!/^\+\d{8,15}$/.test(phone)) throw new Error("not an E.164 phone");
  compose(
    "exec",
    "-T",
    "postgres",
    "sh",
    "-c",
    `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE users SET no_show_count = 3 WHERE phone = '${phone}'"`,
  );
}

/** Inserts appointments copied from `like` (same business, service and resource; the same
 *  customer, or another user when `otherCustomer`), 30 minutes each, at `starts`, with `status`.
 *  Returns their ids. Past, checked-in and someone else's appointments cannot be made through
 *  the API, so the tests write them here (local only). */
export function insertAppointments(like: string, status: string, starts: Date[], otherCustomer = false): string[] {
  requireLocal();
  if (!/^[0-9a-f-]{36}$/.test(like) || !/^[a-z_]+$/.test(status)) throw new Error("bad appointment input");
  const times = starts.map((start) => `'${start.toISOString()}'`).join(",");
  const customer = otherCustomer ? "(SELECT id FROM users WHERE id <> a.customer_id ORDER BY created_at LIMIT 1)" : "a.customer_id";
  const sql =
    "INSERT INTO appointments (business_id, service_id, resource_id, customer_id, during, status) " +
    `SELECT a.business_id, a.service_id, a.resource_id, ${customer}, tstzrange(s.at, s.at + interval '30 minutes'), '${status}' ` +
    `FROM appointments a, unnest(ARRAY[${times}]::timestamptz[]) AS s(at) WHERE a.id = '${like}' RETURNING id`;
  const out = compose(
    "exec", "-T", "postgres", "sh", "-c",
    `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qtA -v ON_ERROR_STOP=1 -c "${sql}"`,
  );
  return out.split("\n").map((line) => line.trim()).filter(Boolean);
}

/** Finished days for the owner's dashboard. The rollup only ever writes days that are over, so
 *  an e2e run has nothing to show without this: the rows go in the same way the appointments
 *  above do, and `days` counts backwards from yesterday in the business's own zone. */
export function insertDailyStats(businessId: string, serviceId: string, days: string[]): number {
  requireLocal();
  for (const id of [businessId, serviceId]) {
    if (!/^[0-9a-f-]{36}$/.test(id)) throw new Error("bad daily_stats input");
  }
  if (days.some((day) => !/^\d{4}-\d{2}-\d{2}$/.test(day))) throw new Error("bad daily_stats day");
  // Two hourly rows per day (the peak-hours report) and the whole-day row (hour NULL) that the
  // wait-time and no-show reports read, as the nightly rollup writes them.
  const rows = days
    .flatMap((day, index) => {
      const hourly = [10, 17].map((hour) => {
        const joined = 6 + hour % 5 + index % 3;
        const noShows = hour === 17 ? 1 : 0;
        return `('${businessId}'::uuid, '${serviceId}'::uuid, '${day}'::date, ${hour}, ${joined}, ${joined - noShows}, ${240 + hour * 7}, ${300 + hour * 9}, ${260 + hour * 6}, ${noShows}, 0)`;
      });
      const joined = [10, 17].reduce((total, hour) => total + 6 + hour % 5 + index % 3, 0);
      return [...hourly, `('${businessId}'::uuid, '${serviceId}'::uuid, '${day}'::date, NULL, ${joined}, ${joined - 1}, 330, 640, 300, 1, 0)`];
    })
    .join(",");
  const sql =
    "INSERT INTO daily_stats (business_id, service_id, stat_date, hour, tokens_joined, tokens_served, " +
    `avg_wait_sec, p90_wait_sec, avg_predicted_wait_sec, no_shows, cancelled) VALUES ${rows} RETURNING id`;
  const out = compose(
    "exec", "-T", "postgres", "sh", "-c",
    `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qtA -v ON_ERROR_STOP=1 -c "${sql}"`,
  );
  return out.split("\n").map((line) => line.trim()).filter(Boolean).length;
}

/** Puts a running-late notice (or its clearing) for one token into the outbox, as the worker's
 *  job would; the relay publishes it to the token's stream. Local only: the job itself is
 *  covered by the backend tests, and waiting for it here would take minutes. */
export function pushRunningLate(tokenId: string, delayMin: number, range: [number, number] | null, cleared = false): void {
  requireLocal();
  if (!/^[0-9a-f-]{36}$/.test(tokenId) || !Number.isInteger(delayMin)) throw new Error("bad running-late input");
  const eta = range ? `jsonb_build_object('min_minutes', ${range[0]}, 'max_minutes', ${range[1]})` : "'null'::jsonb";
  const kind = cleared ? "token.running_late_cleared" : "token.running_late";
  const sql =
    "INSERT INTO outbox_events (business_id, queue_id, event_type, payload) " +
    `SELECT business_id, queue_id, '${kind}', jsonb_build_object('token_id', id::text, 'token_no', token_no, ` +
    `'status', status, 'delay_min', ${delayMin}, 'eta', ${eta}) FROM queue_tokens WHERE id = '${tokenId}'`;
  compose(
    "exec", "-T", "postgres", "sh", "-c",
    `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -v ON_ERROR_STOP=1 -c "${sql}"`,
  );
}

/** Restarts the API container (every open stream drops) and waits until it answers again.
 *  `restart` keeps the container's settings, the e2e override's included (local only). */
export function restartApi(): void {
  requireLocal();
  compose("restart", "api");
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      execFileSync("curl", ["-sf", "-o", "/dev/null", `${API_URL}/healthz`]);
      return;
    } catch {
      execFileSync("sleep", ["1"]);
    }
  }
  throw new Error("the API did not come back");
}

/** The OTP the mock SMS provider logged for a phone ending in `last4` (the API only runs the
 *  mock with APP_ENV local or test, and logs the last four digits only). */
export async function otpCodeFor(last4: string, since: Date): Promise<string> {
  const pattern = new RegExp(`MOCK SMS to \\*\\*\\*${last4}: OTP code (\\d{6})`, "g");
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const logs = compose("logs", "api", "--no-color", "--since", since.toISOString());
    const codes = [...logs.matchAll(pattern)].map((match) => match[1]);
    if (codes.length) return codes[codes.length - 1];
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`no OTP logged for ***${last4}`);
}

/** A phone number nobody else uses in this run (E.164, India). */
export function uniquePhone(): { e164: string; national: string; last4: string } {
  const national = `9${String(Date.now()).slice(-6)}${String(Math.floor(Math.random() * 1000)).padStart(3, "0")}`;
  return { e164: `+91${national}`, national, last4: national.slice(-4) };
}
