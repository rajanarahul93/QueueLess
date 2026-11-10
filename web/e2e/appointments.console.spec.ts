import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { localToday, timeLabel } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, insertAppointments, otpCodeFor, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// The staff appointments page for the console's own business, in a Los Angeles browser against
// a business in another zone. One customer books one real appointment; every other shape it
// needs (too early, too late, cancelled, spare slots) is cloned from it in the database, so the
// job spends one OTP sign-in instead of eight. Staff 3 is this spec's own member, so its
// sign-ins have their own share of the per-email login limit.
test.describe.configure({ mode: "serial" });

type Person = { email: string; password: string };
type Queue = { id: string; name: string; service: string; timezone: string; staff: Person; staff2: Person };
/** This spec's own business: it fills today's grid with cloned appointments, so it must not
 *  share one with the console spec. */
const place = (project: string): Queue => {
  const found = JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8")).appointments[project];
  if (!found?.staff2) throw new Error(`no appointments business with two members for ${project}`);
  return found;
};
const MINUTE = 60_000;

let queue: Queue;
let staffApi: APIRequestContext;
let staffAuth: Record<string, string>;
let queueId: string;
let customerAuth: Record<string, string>;
let board: BrowserContext;
let page: Page;
/** The appointment the customer really booked; every clone is made from it. */
let seed: { id: string; start: string };

const said = (where: Page) => where.getByRole("status");
/** The row in the seed's own slot that still offers the button: the day also holds the clones
 *  that are too early or too late, and whatever already ended in that slot. */
const seedRow = (where: Page) =>
  where
    .getByRole("listitem")
    .filter({ hasText: timeLabel(seed.start, queue.timezone) })
    .filter({ has: where.getByRole("button", { name: "Check in" }) });
const pill = (where: Page, state: string) => where.locator(`[data-pill="${state}"]`);

async function signIn(context: BrowserContext, who: Person): Promise<Page> {
  const signed = await context.newPage();
  await signed.goto("/signin");
  await signed.getByLabel("Email").fill(who.email);
  await signed.getByLabel("Password").fill(who.password);
  await signed.getByRole("button", { name: "Sign in" }).click();
  // A member of one business lands on the picker, which then replaces the URL with that
  // business's console. Waiting for the FINAL landing (/console/<id>, not /console) keeps a
  // later goto from being interrupted by that replace.
  await expect(signed).toHaveURL(new RegExp(`/console/${queue.id}$`));
  return signed;
}

/** Opens the appointments page and waits until it is live, so nothing that follows depends on
 *  how long the stream took to connect (the 6D-2 set-up rule). */
async function openAppointments(where: Page): Promise<void> {
  await where.goto(`/console/${queue.id}/appointments`);
  await expect(where.getByRole("heading", { name: "Appointments today" })).toBeVisible();
  await expect(pill(where, "live")).toBeVisible({ timeout: 30_000 });
}

/** Ends every active token through the API, which also ends its appointment, so the seed's slot
 *  is free for the next flow. A waiting token has to be called before it can be ended: no-show
 *  belongs to a called token, and a refusal here would leave the appointment active. */
async function emptyQueue(): Promise<void> {
  for (let round = 0; round < 6; round += 1) {
    const listed = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
    const tokens = (await listed.json()).tokens as { id: string; status: string; version: number }[];
    if (tokens.length === 0) return;
    for (const token of tokens) {
      if (token.status === "waiting") {
        await staffApi.post(`${API_URL}/v1/queues/${queueId}/call-next`, { headers: staffAuth });
        continue;
      }
      const action = token.status === "in_service" ? "complete" : "no-show";
      await staffApi.post(`${API_URL}/v1/tokens/${token.id}/${action}`, { headers: staffAuth, data: { version: token.version } });
    }
  }
  throw new Error("the queue still has tokens after six rounds");
}

/** The appointments the API lists for today, as the page reads them. */
async function listed(): Promise<{ id: string; status: string; customer: { name: string } }[]> {
  const answer = await staffApi.get(`${API_URL}/v1/appointments?business_id=${queue.id}&limit=50`, { headers: staffAuth });
  expect(answer.ok(), await answer.text()).toBe(true);
  return (await answer.json()).items;
}

const statusOf = async (id: string) => (await listed()).find((item) => item.id === id)?.status;

test.beforeAll(async ({}, testInfo) => {
  queue = place(testInfo.project.name);
  staffApi = await request.newContext();
  const login = await staffApi.post(`${API_URL}/v1/auth/login`, { data: queue.staff });
  expect(login.ok(), await login.text()).toBe(true);
  staffAuth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  queueId = (await (await staffApi.get(`${API_URL}/v1/businesses/${queue.id}/queue`, { headers: staffAuth })).json()).id;

  // One customer, one OTP sign-in, one real booking in the next free slot.
  const phone = uniquePhone();
  const api = await request.newContext();
  const sent = new Date(Date.now() - 1000);
  expect((await api.post(`${API_URL}/v1/auth/otp/request`, { data: { phone: phone.e164 } })).status()).toBe(202);
  const code = await otpCodeFor(phone.last4, sent);
  const verified = await api.post(`${API_URL}/v1/auth/otp/verify`, { data: { phone: phone.e164, code } });
  expect(verified.ok(), await verified.text()).toBe(true);
  customerAuth = { Authorization: `Bearer ${(await verified.json()).access_token}` };
  const today = localToday(queue.timezone);
  const free = await api.get(`${API_URL}/v1/businesses/${queue.id}/availability?service_id=${queue.service}&date=${today}`, { headers: customerAuth });
  const slot = ((await free.json()).slots as { start: string; resource_id: string }[]).find((item) => Date.parse(item.start) >= Date.now());
  expect(slot, "a free slot later today").toBeTruthy();
  const booked = await api.post(`${API_URL}/v1/appointments`, {
    headers: customerAuth,
    data: { service_id: queue.service, resource_id: slot!.resource_id, start: slot!.start },
  });
  expect(booked.status(), await booked.text()).toBe(201);
  seed = { id: (await booked.json()).id, start: slot!.start };
  await api.dispose();
});

test.afterAll(async () => {
  await board?.close();
  await staffApi?.dispose();
});

test("staff see today's appointments in the business zone, names only", async ({ browser }) => {
  board = await browser.newContext();
  await board.addInitScript(countUntitled);
  page = await signIn(board, queue.staff);
  await openAppointments(page);
  await expectTitle(page, "Appointments today · QueueLess");
  // Customers signed up by phone are all called "Customer"; the row shows that name only.
  const row = page.getByRole("listitem").filter({ hasText: "Checkup" });
  await expect(row.first()).toBeVisible();
  // The time the business keeps, not the browser's: the two zones differ by hours.
  await expect(page.getByText(`${timeLabel(seed.start, queue.timezone)}–`, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(queue.timezone, { exact: false }).first()).toBeVisible();
  const text = (await page.locator("main").textContent()) ?? "";
  expect(text).not.toContain(seed.id);
  expect(text).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-/); // no id of any kind
  expect(text).not.toContain("@"); // no email
  await noAxeViolations(page, "/console/[id]/appointments (list)");
});

test("a cancelled appointment shows its label and offers no action", async () => {
  const [cancelled] = insertAppointments(seed.id, "cancelled", [new Date(Date.parse(seed.start) + 90 * MINUTE)]);
  expect(cancelled).toBeTruthy();
  await page.reload();
  await expect(pill(page, "live")).toBeVisible({ timeout: 30_000 });
  const row = page.getByRole("listitem").filter({ hasText: "Cancelled" });
  await expect(row).toBeVisible();
  await expect(row.getByRole("button", { name: "Check in" })).toHaveCount(0);
  await expect(row.getByRole("link", { name: "Open in queue" })).toHaveCount(0);
});

test("too early and too late say what the server refused", async () => {
  // Cloned around the seed: one far ahead, one already over.
  const [early] = insertAppointments(seed.id, "booked", [new Date(Date.now() + 120 * MINUTE)]);
  const [late] = insertAppointments(seed.id, "booked", [new Date(Date.now() - 120 * MINUTE)]);
  await page.reload();
  await expect(pill(page, "live")).toBeVisible({ timeout: 30_000 });
  const rows = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Check in" }) });
  await expect(rows.first()).toBeVisible();
  // The last row of the day is the one two hours ahead; the first is the one two hours ago.
  await rows.first().getByRole("button", { name: "Check in" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText("That appointment time has passed");
  await noAxeViolations(page, "/console/[id]/appointments (too late)");
  await rows.last().getByRole("button", { name: "Check in" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText("Check-in opens 30 minutes before");
  await noAxeViolations(page, "/console/[id]/appointments (too early)");
  // Neither created a token.
  expect(await statusOf(early)).toBe("booked");
  expect(await statusOf(late)).toBe("booked");
  expect(((await (await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth })).json()).tokens as unknown[]).length).toBe(0);
});

test("staff check a customer in: the token shows on the row and in the queue", async () => {
  await page.reload();
  await expect(pill(page, "live")).toBeVisible({ timeout: 30_000 });
  // By its time, not its status: the status is what the check-in is about to change.
  const row = page.getByRole("listitem").filter({ hasText: timeLabel(seed.start, queue.timezone) });
  await row.getByRole("button", { name: "Check in" }).click();
  await expect(said(page)).toContainText("Checked in");
  await expect(row.getByText(/Token #\d+/)).toBeVisible();
  await noAxeViolations(page, "/console/[id]/appointments (checked in)");
  // The API agrees, and the console queue has the token.
  expect(await statusOf(seed.id)).toBe("checked_in");
  const tokens = (await (await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth })).json()).tokens as { source: string }[];
  expect(tokens).toHaveLength(1);
  expect(tokens[0].source).toBe("appointment");
  // "Open in queue" opens the console with that token selected.
  await row.getByRole("link", { name: "Open in queue" }).click();
  await expect(page.getByRole("button", { name: /^#\d+/ }).first()).toHaveAttribute("aria-pressed", "true");
  await emptyQueue();
});

test("a double press checks in once", async () => {
  const [twice] = insertAppointments(seed.id, "booked", [new Date(seed.start)]);
  await openAppointments(page);
  const row = seedRow(page);
  const press = row.getByRole("button", { name: "Check in" });
  await press.dblclick();
  await expect(said(page)).toContainText("Checked in");
  expect(await statusOf(twice)).toBe("checked_in");
  const tokens = (await (await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth })).json()).tokens as unknown[];
  expect(tokens).toHaveLength(1); // one press, one token
  await emptyQueue();
});

test("the customer checks in on their own: the row is held and a click inside sends nothing", async () => {
  const [mine] = insertAppointments(seed.id, "booked", [new Date(seed.start)]);
  await openAppointments(page);
  // Anchored on what does NOT change: earlier flows left no-show rows in this same slot, and
  // this row's own label is about to go from Booked to Checked in.
  const row = page
    .getByRole("listitem")
    .filter({ hasText: timeLabel(seed.start, queue.timezone) })
    .filter({ hasNotText: "No-show" });
  await expect(row.getByRole("button", { name: "Check in" })).toBeEnabled();
  const self = await staffApi.post(`${API_URL}/v1/appointments/${mine}/check-in`, { headers: customerAuth });
  expect([201, 200]).toContain(self.status());
  await expect(said(page)).toContainText("is now checked in", { timeout: 15_000 });
  // The row now says "Open in queue", and for the length of the hold it does nothing: a click
  // already on its way cannot act on an action the staff member never read.
  // Inside the hold the action is a dead button and no link exists, so there is nothing a
  // click could act on. Clicking it here would race the 700 ms expiry (a forced click lands on
  // whatever is at those coordinates once the real link appears); the unit test presses it.
  await expect(row.getByRole("button", { name: "Open in queue" })).toBeDisabled();
  await expect(row.getByRole("link", { name: "Open in queue" })).toHaveCount(0);
  await expect(page).toHaveURL(/\/appointments$/);
  // Once the hold is over the row offers the real link.
  await expect(row.getByRole("link", { name: "Open in queue" })).toBeVisible({ timeout: 5000 });
  await noAxeViolations(page, "/console/[id]/appointments (held)");
  // The API agrees: one token, from the customer's own check-in.
  const tokens = (await (await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth })).json()).tokens as unknown[];
  expect(tokens).toHaveLength(1);
  await emptyQueue();
});

test("two staff press at once: both end with the same token, the second is told", async ({ browser }) => {
  const [shared] = insertAppointments(seed.id, "booked", [new Date(seed.start)]);
  await openAppointments(page);
  const other = await browser.newContext();
  const otherPage = await signIn(other, queue.staff2);
  await openAppointments(otherPage);
  await Promise.all([
    seedRow(page).getByRole("button", { name: "Check in" }).click(),
    seedRow(otherPage).getByRole("button", { name: "Check in" }).click(),
  ]);
  for (const where of [page, otherPage]) {
    await expect(said(where)).toContainText(/Checked in|Already checked in/, { timeout: 15_000 });
    await expect(where.locator("main").getByRole("alert")).toHaveCount(0); // a repeat is not an error
  }
  expect(await statusOf(shared)).toBe("checked_in");
  const tokens = (await (await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth })).json()).tokens as unknown[];
  expect(tokens).toHaveLength(1);
  await other.close();
  await emptyQueue();
});

test("one staff member's check-in shows on another's page", async ({ browser }) => {
  const [watched] = insertAppointments(seed.id, "booked", [new Date(seed.start)]);
  await openAppointments(page);
  const other = await browser.newContext();
  const otherPage = await signIn(other, queue.staff2);
  await openAppointments(otherPage);
  const asked = Date.now();
  const checked = await staffApi.post(`${API_URL}/v1/appointments/${watched}/check-in`, { headers: staffAuth });
  expect(checked.status()).toBe(201);
  const number = (await checked.json()).token_no;
  await expect(otherPage.getByText(`Token #${number}`)).toBeVisible({ timeout: 15_000 });
  console.log(`appointments: a check-in showed on the other screen in ${Date.now() - asked} ms`);
  await other.close();
  await emptyQueue();
});

test("the empty state", async () => {
  // Every appointment of the day ends: the page says there is nothing left to do.
  for (const item of await listed()) {
    if (item.status === "booked") {
      await staffApi.delete(`${API_URL}/v1/appointments/${item.id}`, { headers: customerAuth });
    }
  }
  await page.reload();
  await expect(pill(page, "live")).toBeVisible({ timeout: 30_000 });
  const empty = page.getByText("No appointments today");
  if (await empty.count()) {
    await expect(empty).toBeVisible();
    await noAxeViolations(page, "/console/[id]/appointments (empty)");
  } else {
    // Appointments that already ended still belong to today and keep their label.
    await expect(page.getByRole("listitem").first()).toBeVisible();
  }
});

test("the access-ended and not-yours copy", async () => {
  await page.goto(`/console/00000000-0000-0000-0000-000000000000/appointments`);
  await expect(page.getByRole("heading", { name: "You don't have access to this business." })).toBeVisible();
  await noAxeViolations(page, "/console/[id]/appointments (not yours)");
});

// 6E-1: this spec's staff member is already signed in, so the owner-screen refusal costs none
// of the 20 logins an IP gets every 15 minutes. Last in the file: nothing after it needs `page`.
test("the owner screens refuse a staff member", async () => {
  for (const path of ["/owner", "/owner/new"]) {
    await page.goto(path);
    await expect(page.getByText("You don't have access to this page")).toBeVisible();
    await expect(page.getByRole("heading", { level: 1, name: "Your business" })).toHaveCount(0);
  }
  await noAxeViolations(page, "/owner (staff refused)");
});
