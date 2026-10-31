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
