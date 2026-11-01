import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { timeLabel } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, otpCodeFor, pushRunningLate, restartApi, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// The live ticket in a Los Angeles browser against a business in another zone (Kolkata unless
// that is near midnight; global setup), with a staff member
// acting through the API (the staff screens come later). Each browser project has its own
// queue business and staff account (global setup), so call-next only ever serves this test's
// customer. The e2e stack ends streams after 20 s (its Compose override) to show reconnects.
test.describe.configure({ mode: "serial" });

type Queue = { id: string; name: string; service: string; timezone: string; staff: { email: string; password: string } };
const place = (project: string): Queue => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8")).queues[project];

let queue: Queue;
let customer: BrowserContext;
let staffApi: APIRequestContext;
let staffAuth: Record<string, string>;
let queueId: string;
let ticketId: string;
let firstNumber: number; // other specs may use the same queue first

/** Init script: remembers every pill state the page ever showed (to prove "never polling"). */
function recordPills() {
  addEventListener("DOMContentLoaded", () => {
    const seen = new Set<string>();
    (window as unknown as { pills: Set<string> }).pills = seen;
    new MutationObserver(() => {
      document.querySelectorAll("[data-pill]").forEach((pill) => seen.add(pill.getAttribute("data-pill") ?? ""));
    }).observe(document.body, { subtree: true, childList: true, attributes: true });
  });
}
const pillsSeen = (page: Page) => page.evaluate(() => [...(window as unknown as { pills: Set<string> }).pills]);
const pill = (page: Page, state: string) => page.locator(`[data-pill="${state}"]`);

async function bearer(context: BrowserContext): Promise<Record<string, string>> {
  const refreshed = await context.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok()).toBe(true);
  return { Authorization: `Bearer ${(await refreshed.json()).access_token}` };
}

/** The customer joins as a walk-in through the API (the Join button comes with the next part). */
async function join(): Promise<{ id: string; joined_at: string; token_no: number }> {
  const joined = await customer.request.post(`${API_URL}/v1/queues/${queueId}/tokens`, { headers: await bearer(customer), data: {} });
  expect(joined.status()).toBe(201);
  return joined.json();
}

async function staff(action: string, target?: { id: string; version: number }, body: object = {}) {
  const url = target ? `${API_URL}/v1/tokens/${target.id}/${action}` : `${API_URL}/v1/queues/${queueId}/call-next`;
  const answer = await staffApi.post(url, { headers: staffAuth, data: target ? { version: target.version, ...body } : undefined });
  expect(answer.status(), await answer.text()).toBe(200);
  return (await answer.json()) as { id: string; version: number; status: string };
}

const ticketTitle = (no: number) => `Ticket #${no} at ${queue.name} · QueueLess`;

test.beforeAll(async ({}, testInfo) => {
  queue = place(testInfo.project.name);
  staffApi = await request.newContext();
  const login = await staffApi.post(`${API_URL}/v1/auth/login`, { data: queue.staff });
  expect(login.ok()).toBe(true);
  staffAuth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const found = await staffApi.get(`${API_URL}/v1/businesses/${queue.id}/queue`, { headers: staffAuth });
  queueId = (await found.json()).id;
});

test.afterAll(async () => {
  await staffApi?.dispose();
});

test("a signed-in customer's ticket opens from /ticket, in the business's time", async ({ browser }) => {
  customer = await browser.newContext();
  await customer.addInitScript(countUntitled);
  await customer.addInitScript(recordPills);
  const page = await customer.newPage();
  await page.goto("/ticket");
  const phone = uniquePhone();
  await page.getByLabel("Mobile number").fill(phone.national);
  const sent = new Date(Date.now() - 1000);
  await page.getByRole("button", { name: "Send code" }).click();
  const code = await otpCodeFor(phone.last4, sent);
  await page.getByLabel("Digit 1").click();
  await page.keyboard.type(code);
  await expect(page.getByText("You have no active tickets.")).toBeVisible();
  await expectTitle(page, "Your tickets · QueueLess");
  await noAxeViolations(page, "/ticket (none)");

  const ticket = await join();
  ticketId = ticket.id;
  firstNumber = ticket.token_no;
  await page.reload(); // resume: one active ticket opens directly
  await expect(page).toHaveURL(new RegExp(`/ticket/${ticket.id}$`));
  await expect(page.getByText("You're next")).toBeVisible();
  await expect(pill(page, "live")).toBeVisible();
  // the browser is in Los Angeles; the time shown is the business's
  await expect(page.getByText(`Joined at ${timeLabel(ticket.joined_at, queue.timezone)} (${queue.timezone})`)).toBeVisible();
  await expectTitle(page, ticketTitle(ticket.token_no));
  await noAxeViolations(page, "/ticket/[id] (next)");
});

test("staff calls next: 'It's your turn' within 2 s, then in service and completed; the stream closes", async () => {
  const page = customer.pages()[0];
  const started = Date.now();
  const called = await staff("call-next");
  await expect(page.getByRole("heading", { name: "It's your turn" })).toBeVisible({ timeout: 2000 });
  const measured = Date.now() - started;
  test.info().annotations.push({ type: "time to It's your turn (ms)", description: String(measured) });
  console.log(`time to "It's your turn": ${measured} ms (${test.info().project.name})`);
  await expect(page.getByRole("heading", { name: "It's your turn" })).toBeFocused();
  await expect(page.locator('main [aria-live="assertive"]')).toHaveText("It's your turn — please go to the counter.");
  await noAxeViolations(page, "/ticket/[id] (called)");

  const serving = await staff("start", called, { service_id: queue.service });
  await expect(page.getByText("You're being served.")).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (in service)");
  const closed = page.waitForEvent("requestfailed", (failed) => failed.url().includes("/stream?"));
  await staff("complete", serving);
  await expect(page.getByRole("heading", { name: "You've been served. Thanks for visiting." })).toBeFocused();
  await expect(page.getByRole("link", { name: "Back to business" })).toHaveAttribute("href", `/b/${queue.id}`);
  await closed; // the stream was closed by the page, not left open
  await expectTitle(page, ticketTitle(firstNumber));
  await noAxeViolations(page, "/ticket/[id] (completed)");
});

test("a no-show ends the ticket", async () => {
  const page = customer.pages()[0];
  const ticket = await join();
  await page.goto(`/ticket/${ticket.id}`);
  await expect(pill(page, "live")).toBeVisible();
  await staff("no-show", await staff("call-next"));
  await expect(page.getByRole("heading", { name: "You were marked as not here when called." })).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (no-show)");
});

test("leave asks first (Esc stays), then cancels the ticket", async () => {
  const page = customer.pages()[0];
  const ticket = await join();
  await page.goto(`/ticket/${ticket.id}`);
  const leave = page.getByRole("button", { name: "Leave the queue" });
  await leave.click();
  await noAxeViolations(page, "/ticket/[id] (leave dialog)");
  await page.keyboard.press("Escape");
  await expect(leave).toBeFocused();
  await leave.click();
  await page.getByRole("button", { name: "Yes, leave" }).click();
  await expect(page.getByRole("heading", { name: "This ticket is cancelled." })).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (cancelled)");
});

test("running late shows the delay and the new range, and clears", async () => {
  const page = customer.pages()[0];
  const ticket = await join();
  ticketId = ticket.id;
  await page.goto(`/ticket/${ticket.id}`);
  await expect(pill(page, "live")).toBeVisible();
  pushRunningLate(ticket.id, 15, [20, 30]);
  const notice = page.getByText("Running about 15 min behind. Your new estimate is 20–30 min.");
  await expect(notice).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (running late)");
  pushRunningLate(ticket.id, 0, null, true);
  await expect(notice).toHaveCount(0);
});

test("the server's 20 s stream lifetime: a new stream token and Last-Event-ID, never polling", async () => {
  test.setTimeout(60_000);
  const page = customer.pages()[0];
  const streams: { issued: string | null; lastId: string | undefined }[] = [];
  page.on("request", (sent) => {
    if (sent.url().includes(`/v1/tokens/${ticketId}/stream?`)) {
      streams.push({ issued: new URL(sent.url()).searchParams.get("stream_token"), lastId: sent.headers()["last-event-id"] });
    }
  });
  await page.reload();
  await expect(pill(page, "live")).toBeVisible();
  await expect.poll(() => streams.length, { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
  expect(streams[1].issued).not.toBe(streams[0].issued);
  expect(streams[1].lastId).toMatch(/^\d+$/);
  await expect(pill(page, "live")).toBeVisible();
  expect(await pillsSeen(page)).not.toContain("polling");
  expect(await pillsSeen(page)).not.toContain("reconnecting");
});

test("offline keeps the numbers, says so, and recovers when back online", async () => {
  const page = customer.pages()[0];
  await customer.setOffline(true);
  await expect(pill(page, "offline")).toHaveText("Offline — retrying");
  await expect(page.getByText(/May be out of date/)).toBeVisible();
  await expect(page.getByText("You're next")).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (offline)");
  await customer.setOffline(false);
  await expect(pill(page, "live")).toBeVisible();
});

test("two tabs of one customer both see the call", async () => {
  const [first] = customer.pages();
  const second = await customer.newPage();
  await second.goto(`/ticket/${ticketId}`);
  await expect(pill(second, "live")).toBeVisible();
  await staff("call-next");
  for (const page of [first, second]) await expect(page.getByRole("heading", { name: "It's your turn" })).toBeVisible();
  await second.close();
});

test("a sixth open screen is told 'Too many open screens' and polls", async () => {
  const pages = [customer.pages()[0]];
  for (let count = 1; count < 6; count += 1) {
    const page = await customer.newPage();
    await page.goto(`/ticket/${ticketId}`);
    pages.push(page);
    if (count < 5) await expect(pill(page, "live")).toBeVisible();
  }
  const sixth = pages[5];
  await expect(sixth.getByText(/Too many open screens/)).toBeVisible();
  await expect(pill(sixth, "capped")).toHaveText("Live (slow)");
  await noAxeViolations(sixth, "/ticket/[id] (too many screens)");
  for (const page of pages.slice(1)) await page.close();
});

test("a real API restart: reconnecting, then live again", async () => {
  test.setTimeout(120_000);
  const page = customer.pages()[0];
  await expect(pill(page, "live")).toBeVisible();
  restartApi();
  await expect(page.locator('[data-pill="reconnecting"], [data-pill="offline"], [data-pill="polling"]')).toBeVisible();
  await noAxeViolations(page, "/ticket/[id] (reconnecting)");
  await expect(pill(page, "live")).toBeVisible({ timeout: 90_000 }); // the stream is retried every 60 s
  await expect(page.getByRole("heading", { name: "It's your turn" })).toBeVisible();
});

test("two stream failures: polling every 10 s ('Live (slow)'), and an update arrives by polling", async () => {
  const page = customer.pages()[0];
  let failed = 0;
  await customer.route("**/v1/tokens/*/stream?*", (route) => {
    failed += 1;
    return route.fulfill({ status: 500, body: "{}" });
  });
  await page.reload();
  await expect(pill(page, "polling")).toHaveText("Live (slow)");
  expect(failed).toBe(2);
  await noAxeViolations(page, "/ticket/[id] (polling)");
  const called = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
  const mine = ((await called.json()).tokens as { id: string; version: number }[]).find((entry) => entry.id === ticketId)!;
  await staff("start", mine, { service_id: queue.service });
  await expect(page.getByText("You're being served.")).toBeVisible({ timeout: 12_000 });
  await customer.unrouteAll({ behavior: "wait" });
});

test("a tab hidden for 5 minutes closes its stream; back in view it reconnects, not as a failure", async () => {
  const page = customer.pages()[0];
  await page.clock.install();
  await page.reload();
  await expect(pill(page, "live")).toBeVisible();
  const closed = page.waitForEvent("requestfailed", (failed) => failed.url().includes("/stream?"));
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.fastForward("05:01");
  await closed;
  const reopened = page.waitForRequest((sent) => sent.url().includes("/stream?"));
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await reopened;
  await expect(pill(page, "live")).toBeVisible();
  expect(await pillsSeen(page)).not.toContain("polling");
  await customer.close();
});
