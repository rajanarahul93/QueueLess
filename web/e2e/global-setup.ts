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

/** The API's staff-creation body for a throwaway account (the API names the field). */
const staffBody = (person: { email: string; password: string }) => ({
  name: "E2E Staff",
  email: person.email,
  initial_password: person.password,
});

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
    staffBody(staff),
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
  // A business whose appointments cannot be moved within 7 days of their start (the maximum).
  const strict = await post(
    "/v1/businesses",
    { name: `E2E Cutoff ${stamp}`, category: "clinic", timezone: "Asia/Kolkata", open_hours: morning },
    token,
  );
  const strictService = await post(
    `/v1/businesses/${strict.id}/services`,
    { name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00" },
    token,
  );
  const patched = await fetch(`${API_URL}/v1/businesses/${strict.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ settings: { reschedule_cutoff_min: 10080 } }),
  });
  if (!patched.ok) throw new Error(`cutoff settings: ${patched.status} ${await patched.text()}`);
  // One queue business per browser project, open all day, each with its own staff member who
  // acts through the API (call next, start, complete): call-next then only serves that
  // project's customer. The staff sign-in reuses this run's throwaway password (state file only).
  const queues: Record<string, { id: string; name: string; service: string; staff: typeof staff }> = {};
  for (const project of ["chromium", "firefox", "webkit"]) {
    const desk = await post(
      "/v1/businesses",
      { name: `E2E Queue ${project} ${stamp}`, category: "clinic", timezone: "Asia/Kolkata", open_hours: everyDay },
      token,
    );
    const work = await post(
      `/v1/businesses/${desk.id}/services`,
      { name: "Checkup", duration_min: 30, buffer_min: 0, price: "250.00" },
      token,
    );
    const member = { ...staff, email: `e2e-queue-${project}-${stamp}@example.test` };
    await post(`/v1/businesses/${desk.id}/staff`, { ...staffBody(member), name: "E2E Queue Staff" }, token);
    queues[project] = { id: desk.id, name: desk.name, service: work.id, staff: member };
  }
  mkdirSync(join(__dirname, ".state"), { recursive: true });
  writeFileSync(
    ACCOUNTS_FILE,
    JSON.stringify({
      owner,
      staff,
      booking: { id: clinic.id, name: clinic.name, service: service.id },
      cutoff: { id: strict.id, name: strict.name, service: strictService.id },
      queues,
    }),
  );
}
