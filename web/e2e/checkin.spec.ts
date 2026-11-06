import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dayLabel, localToday, timeLabel } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, insertAppointments, otpCodeFor, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// Check in and walk in, with a staff member acting through the API (the staff screens come
// later). Same per-project queue business as the ticket spec; this spec leaves no waiting
// ticket behind, so that spec's call-next still serves only its own customer. Appointments are
// placed relative to now on the business's own day (global setup keeps it away from midnight),
// so nothing depends on the clock beyond "today".
test.describe.configure({ mode: "serial" });

type Queue = { id: string; name: string; service: string; timezone: string; staff: { email: string; password: string } };
const place = (project: string): Queue => {
  const found = JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8")).queues[project];
  if (!found) throw new Error(`no queue business for ${project}: set E2E_BROWSERS to include it`);
  return found;
};
const MINUTE = 60_000;

let queue: Queue;
let customer: BrowserContext;
let staffApi: APIRequestContext;
let staffAuth: Record<string, string>;
let queueId: string;
const ids: Record<string, string> = {};
const starts: Record<string, string> = {};

async function bearer(context: BrowserContext): Promise<Record<string, string>> {
  const refreshed = await context.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok()).toBe(true);
  return { Authorization: `Bearer ${(await refreshed.json()).access_token}` };
}

/** Books the first free slot today starting at least `ahead` from now; returns its id and start. */
async function bookToday(ahead: number): Promise<{ id: string; start: string }> {
  const headers = await bearer(customer);
  const today = localToday(queue.timezone);
  const free = await customer.request.get(
    `${API_URL}/v1/businesses/${queue.id}/availability?service_id=${queue.service}&date=${today}`,
    { headers },
  );
  const slot = (await free.json()).slots.find((item: { start: string }) => Date.parse(item.start) >= Date.now() + ahead);
  expect(slot, "a free slot later today").toBeTruthy();
  const booked = await customer.request.post(`${API_URL}/v1/appointments`, {
    headers,
    data: { service_id: queue.service, resource_id: slot.resource_id, start: slot.start },
  });
  expect(booked.status()).toBe(201);
  return { id: (await booked.json()).id, start: slot.start };
}

async function staff(action: string, target?: { id: string; version: number }) {
  const url = target ? `${API_URL}/v1/tokens/${target.id}/${action}` : `${API_URL}/v1/queues/${queueId}/call-next`;
  const answer = await staffApi.post(url, { headers: staffAuth, data: target ? { version: target.version } : undefined });
  expect(answer.status(), await answer.text()).toBe(200);
  return (await answer.json()) as { id: string; version: number };
}

/** The Check in button of the appointment starting at `start` (its accessible name says which). */
const checkInButton = (page: Page, start: string) => {
  const day = dayLabel(localToday(queue.timezone, new Date(start)), "long");
  return page.getByRole("button", { name: `Check in Checkup on ${day} at ${timeLabel(start, queue.timezone)}` });
};
const alertNear = (button: ReturnType<Page["getByRole"]>) => button.locator("xpath=following-sibling::*[@role='alert'][1]");

test.beforeAll(async ({}, testInfo) => {
  queue = place(testInfo.project.name);
  staffApi = await request.newContext();
  const login = await staffApi.post(`${API_URL}/v1/auth/login`, { data: queue.staff });
  expect(login.ok()).toBe(true);
  staffAuth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  queueId = (await (await staffApi.get(`${API_URL}/v1/businesses/${queue.id}/queue`, { headers: staffAuth })).json()).id;
});

test.afterAll(async () => {
  await customer?.close();
  await staffApi?.dispose();
});

test("today's appointment inside the window shows Check in on My appointments", async ({ browser }) => {
  customer = await browser.newContext();
  await customer.addInitScript(countUntitled);
  const page = await customer.newPage();
  await page.goto("/me");
  const phone = uniquePhone();
  await page.getByLabel("Mobile number").fill(phone.national);
  const sent = new Date(Date.now() - 1000);
  await page.getByRole("button", { name: "Send code" }).click();
  const code = await otpCodeFor(phone.last4, sent);
  await page.getByLabel("Digit 1").click();
  await page.keyboard.type(code);
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();

  ({ id: ids.now, start: starts.now } = await bookToday(0)); // next slot of a 15-minute grid: inside the window
  await page.reload();
  await expect(checkInButton(page, starts.now)).toBeVisible();
  await expectTitle(page, "My appointments · QueueLess");
  await noAxeViolations(page, "/me (check in)");
});

test("too early: check-in opens 30 minutes before the appointment time", async () => {
  const page = customer.pages()[0];
  ({ id: ids.early, start: starts.early } = await bookToday(60 * MINUTE));
  await page.reload();
  const early = checkInButton(page, starts.early);
  await early.click();
  await expect(alertNear(early)).toHaveText("Check-in opens 30 minutes before your appointment time.");
  await noAxeViolations(page, "/me (too early)");
});

test("not booked any more: the stale button explains and the list refreshes", async () => {
  const page = customer.pages()[0];
  const cancelled = await customer.request.delete(`${API_URL}/v1/appointments/${ids.early}`, { headers: await bearer(customer) });
  expect(cancelled.status()).toBe(204);
  await checkInButton(page, starts.early).click(); // the page still shows it
  await expect(page.getByRole("alert").filter({ hasText: "can't be checked in" })).toHaveText(
    "This appointment can't be checked in: it was cancelled, moved or already checked in.",
  );
  await expect(checkInButton(page, starts.early)).toHaveCount(0); // the refreshed list no longer has it
  await expect(checkInButton(page, starts.now)).toBeVisible();
});

test("too late: the slot has ended; the way on is a walk-in", async () => {
  const page = customer.pages()[0];
  // A booked appointment whose 30 minutes ended half an hour ago (the API cannot make one).
  const ended = new Date(Date.now() - 60 * MINUTE);
  [ids.late] = insertAppointments(ids.now, "booked", [ended]);
  await page.reload();
  const late = checkInButton(page, ended.toISOString()); // listed under Past, still today
  await late.click();
  const said = alertNear(late);
  await expect(said).toContainText("Your appointment time has passed. You can still join the queue as a walk-in.");
  await expect(said.getByRole("link", { name: `Join the queue at ${queue.name}` })).toHaveAttribute("href", `/b/${queue.id}`);
  await noAxeViolations(page, "/me (too late)");
});

test("a double press checks in once; the ticket opens; staff call, start and complete it", async () => {
  const page = customer.pages()[0];
  await page.route("**/check-in", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  const posts: string[] = [];
  page.on("request", (sent) => {
    if (sent.method() === "POST" && sent.url().endsWith("/check-in")) posts.push(sent.url());
  });
  await checkInButton(page, starts.now).dblclick();
  await expect(page).toHaveURL(/\/ticket\/[0-9a-f-]{36}$/);
  await page.unrouteAll({ behavior: "wait" });
  expect(posts).toHaveLength(1);
  ids.ticket = page.url().split("/").at(-1)!;
  await expectTitle(page, new RegExp(`^Ticket #\\d+ at ${queue.name} · QueueLess$`));

  // Already in the queue: Join on the business page links to this ticket, not a dead end.
  await page.goto(`/b/${queue.id}`);
  await page.getByRole("button", { name: "Join the queue" }).click();
  const already = page.getByRole("alert").filter({ hasText: "You already have a ticket in this queue." });
  await expect(already.getByRole("link", { name: "Open your ticket" })).toHaveAttribute("href", `/ticket/${ids.ticket}`);
  await noAxeViolations(page, "/b/[id] (already in the queue)");

  await already.getByRole("link", { name: "Open your ticket" }).click();
  const called = await staff("call-next");
  expect(called.id).toBe(ids.ticket);
  await expect(page.getByRole("heading", { name: "It's your turn" })).toBeVisible();
  const serving = await staff("start", called);
  await expect(page.getByText("You're being served.")).toBeVisible();
  await staff("complete", serving);
  await expect(page.getByRole("heading", { name: "You've been served. Thanks for visiting." })).toBeVisible();
});

test("walk in: a double press joins once; staff call it; the customer leaves", async () => {
  const page = customer.pages()[0];
  await page.goto(`/b/${queue.id}`);
  await expectTitle(page, `${queue.name} · QueueLess`);
  await noAxeViolations(page, "/b/[id] (join)");
  await page.route("**/v1/queues/*/tokens", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  const posts: string[] = [];
  page.on("request", (sent) => {
    if (sent.method() === "POST" && /\/v1\/queues\/[^/]+\/tokens$/.test(sent.url())) posts.push(sent.url());
  });
  await page.getByRole("button", { name: "Join the queue" }).dblclick();
  await expect(page).toHaveURL(/\/ticket\/[0-9a-f-]{36}$/);
  await page.unrouteAll({ behavior: "wait" });
  expect(posts).toHaveLength(1);
  await expect(page.getByText("You're next")).toBeVisible();
  await staff("call-next");
  await expect(page.getByRole("heading", { name: "It's your turn" })).toBeVisible();
  await page.getByRole("button", { name: "Leave the queue" }).click();
  await page.getByRole("button", { name: "Yes, leave" }).click();
  await expect(page.getByRole("heading", { name: "This ticket is cancelled." })).toBeVisible();
});

test("the hourly limit (a routed 429): the words and a Retry-After countdown", async () => {
  const page = customer.pages()[0];
  await page.goto(`/b/${queue.id}`);
  await page.route("**/v1/queues/*/tokens", (route) =>
    route.fulfill({
      status: 429,
      // the same CORS headers the API sends, or the browser hides Retry-After from the page
      headers: {
        "Retry-After": "5",
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": new URL(page.url()).origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Expose-Headers": "Retry-After, Idempotent-Replayed",
      },
      body: JSON.stringify({ code: "RATE_LIMITED" }),
    }),
  );
  await page.getByRole("button", { name: "Join the queue" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "many times this hour" })).toHaveText(
    /^You've joined or checked in many times this hour\. Try again in [1-5] s\.$/,
  );
  await expect(page.getByRole("button", { name: /^Join the queue \(wait [1-5] s\)$/ })).toBeDisabled();
  await noAxeViolations(page, "/b/[id] (hourly limit)");
  await expect(page.getByRole("button", { name: "Join the queue" })).toBeEnabled({ timeout: 7000 });
  await page.unrouteAll({ behavior: "wait" });
});

test("signed out, Join leads to sign-in and back; staff are told it is for customers", async ({ browser }) => {
  const visitor = await browser.newContext();
  const page = await visitor.newPage();
  await page.goto(`/b/${queue.id}`);
  await page.getByRole("link", { name: "Sign in to join the queue" }).click();
  await expect(page).toHaveURL(new RegExp(`/login\\?next=%2Fb%2F${queue.id}$`));
  await visitor.close();

  const member = await browser.newContext();
  const desk = await member.newPage();
  await desk.goto("/signin");
  await desk.getByLabel("Email").fill(queue.staff.email);
  await desk.getByLabel("Password").fill(queue.staff.password);
  await desk.getByRole("button", { name: "Sign in" }).click();
  // One business, so the picker opens its console straight away (6D-1).
  await expect(desk).toHaveURL(/\/console\/[0-9a-f-]{36}$/);
  await desk.goto(`/b/${queue.id}`);
  await expect(desk.getByText("Joining the queue is for customer accounts.")).toBeVisible();
  await expect(desk.getByRole("button", { name: "Join the queue" })).toHaveCount(0);
  await noAxeViolations(desk, "/b/[id] (staff)");
  await member.close();
});
