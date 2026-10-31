import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { API_URL, clearRateLimits } from "./local-stack";

export const ACCOUNTS_FILE = join(__dirname, ".state", "accounts.json");

async function post(path: string, body: unknown, token?: string) {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

/** Before the run: clear the local rate-limit buckets, then one owner (with a business) and
 *  one staff member for the sign-in tests: 1 register and 1 login per run. */
export default async function globalSetup() {
  clearRateLimits();
  const stamp = Date.now();
  // Throwaway accounts: a fresh random password each run, kept only in the ignored state file.
  const owner = { email: `e2e-owner-${stamp}@example.test`, password: crypto.randomUUID() };
  await post("/v1/auth/register", { name: "E2E Owner", ...owner });
  const { access_token: token } = await post("/v1/auth/login", owner);
  const everyDay = Object.fromEntries(
    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((day) => [day, [{ open: "00:00", close: "23:59" }]]),
  );
  const business = await post(
    "/v1/businesses",
    { name: "E2E Clinic", category: "clinic", timezone: "Asia/Kolkata", open_hours: everyDay },
    token,
  );
  const staff = { email: `e2e-staff-${stamp}@example.test`, password: crypto.randomUUID() };
  await post(
    `/v1/businesses/${business.id}/staff`,
    { name: "E2E Staff", email: staff.email, initial_password: staff.password },
    token,
  );
  // The booking business: Kolkata, 09:00-13:00 every day, one 30-minute service (8 slots a day).
  const morning = Object.fromEntries(
    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((day) => [day, [{ open: "09:00", close: "13:00" }]]),
  );
  const clinic = await post(
    "/v1/businesses",
    { name: `E2E Booking ${stamp}`, category: "clinic", timezone: "Asia/Kolkata", open_hours: morning },
    token,
  );
  const service = await post(
    `/v1/businesses/${clinic.id}/services`,
    { name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00" },
    token,
  );
  mkdirSync(join(__dirname, ".state"), { recursive: true });
  writeFileSync(
    ACCOUNTS_FILE,
    JSON.stringify({ owner, staff, booking: { id: clinic.id, name: clinic.name, service: service.id } }),
  );
}
