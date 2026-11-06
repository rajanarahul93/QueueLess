import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, otpCodeFor, restartApi, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// The staff console in a Los Angeles browser against a business in another zone (Kolkata unless
// that is near midnight; global setup). Customers and staff actions go through the API: this
// stage of the console only reads the queue and sets its status. The e2e stack ends streams
// after 20 s (its Compose override) to show reconnects.
test.describe.configure({ mode: "serial" });

type Queue = { id: string; name: string; service: string; timezone: string; staff: { email: string; password: string } };
type Accounts = { owner: { email: string; password: string }; consoles: Record<string, Queue> };
const accounts = (): Accounts => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));
/** The console's own business: this spec pauses it, closes it for the day and has its staff
 *  member removed, so it must not share the queue the customer specs use. */
const place = (project: string): Queue => {
  const found = accounts().consoles[project];
  if (!found) throw new Error(`no console business for ${project}: set E2E_BROWSERS to include it`);
  return found;
};

let queue: Queue;
let staffApi: APIRequestContext;
let staffAuth: Record<string, string>;
let ownerApi: APIRequestContext;
let ownerAuth: Record<string, string>;
let queueId: string;
let staffUserId: string;
let console_: BrowserContext;
let page: Page;

const pill = (where: Page, state: string) => where.locator(`[data-pill="${state}"]`);
const rows = (where: Page) => where.locator("main ul li");

async function signIn(context: BrowserContext, who: { email: string; password: string }): Promise<Page> {
  const signed = await context.newPage();
  await signed.goto("/signin");
  await signed.getByLabel("Email").fill(who.email);
  await signed.getByLabel("Password").fill(who.password);
  await signed.getByRole("button", { name: "Sign in" }).click();
  // Wait until it has landed: the refresh cookie is set only once the sign-in finishes, and a
  // navigation before that would arrive without a session.
  await expect(signed).not.toHaveURL(/\/signin/);
  return signed;
}

/** A new customer who joins this queue through the API. */
async function joinOne(): Promise<{ status: number; token_no?: number; code?: string }> {
  const phone = uniquePhone();
  const context = await request.newContext();
  const sent = new Date(Date.now() - 1000);
  const requested = await context.post(`${API_URL}/v1/auth/otp/request`, { data: { phone: phone.e164 } });
  expect(requested.status()).toBe(202);
  const code = await otpCodeFor(phone.last4, sent); // the mock provider logs it (local only)
  const verified = await context.post(`${API_URL}/v1/auth/otp/verify`, { data: { phone: phone.e164, code } });
  expect(verified.ok()).toBe(true);
  const token = (await verified.json()).access_token;
  const joined = await context.post(`${API_URL}/v1/queues/${queueId}/tokens`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {},
  });
  const body = await joined.json().catch(() => ({}));
  await context.dispose();
  return { status: joined.status(), token_no: body.token_no, code: body.code };
}

test.beforeAll(async ({}, testInfo) => {
  queue = place(testInfo.project.name);
  ownerApi = await request.newContext();
  const asOwner = await ownerApi.post(`${API_URL}/v1/auth/login`, { data: accounts().owner });
  expect(asOwner.ok(), await asOwner.text()).toBe(true);
  ownerAuth = { Authorization: `Bearer ${(await asOwner.json()).access_token}` };
  const found = await ownerApi.get(`${API_URL}/v1/businesses/${queue.id}/queue`, { headers: ownerAuth });
  queueId = (await found.json()).id;
});

test.afterAll(async () => {
  await ownerApi?.dispose(); // staffApi is the browser context's own request object
  await console_?.close();
});

test("a staff member with one business lands on its console, live", async ({ browser }) => {
  console_ = await browser.newContext();
  await console_.addInitScript(countUntitled);
  page = await signIn(console_, queue.staff);
  // One business: the picker never asks, it opens the console.
  await expect(page).toHaveURL(new RegExp(`/console/${queue.id}$`));
  // The staff member's API calls below reuse this session's cookie: no second password login.
  // Only once the console is up, so the sign-in has certainly set the refresh cookie.
  const refreshed = await console_.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok(), await refreshed.text()).toBe(true);
  const session = await refreshed.json();
  staffAuth = { Authorization: `Bearer ${session.access_token}` };
  staffUserId = session.user.id;
  staffApi = console_.request;
  await expect(page.getByRole("heading", { name: queue.name })).toBeVisible();
  await expectTitle(page, "Queue console · QueueLess");
  await expect(page.getByText("Open", { exact: true }).first()).toBeVisible();
  await expect(pill(page, "live")).toBeVisible();
  await expect(page.getByText("Queue is empty.")).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("0 waiting");
  await noAxeViolations(page, "/console/[id] (empty)");
});

test("a customer who joins appears live, with the service not chosen yet", async () => {
  const started = Date.now();
  const joined = await joinOne();
  expect(joined.status).toBe(201);
  await expect(rows(page)).toHaveCount(1);
  const shown = Date.now() - started;
  console.log(`console: a join appeared in ${shown} ms`);
  expect(shown).toBeLessThan(4000);
  const row = rows(page).first();
  await expect(row.getByText(`#${joined.token_no}`)).toBeVisible();
  await expect(row.getByText("Walk-in")).toBeVisible();
  await expect(row.getByText("Service not chosen yet")).toBeVisible();
  await expect(row.getByText(new RegExp(`\\(${queue.timezone}\\)`))).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("1 waiting");
  await noAxeViolations(page, "/console/[id] (with rows)");
});

test("a staff call through the API moves the row within 2 s, and the service shows after Start", async () => {
  const started = Date.now();
  const called = await staffApi.post(`${API_URL}/v1/queues/${queueId}/call-next`, { headers: staffAuth });
  expect(called.status()).toBe(200);
  const token = await called.json();
  await expect(rows(page).first().getByText("Called")).toBeVisible({ timeout: 2000 });
  const moved = Date.now() - started;
  console.log(`console: a call showed in ${moved} ms`);
  expect(moved).toBeLessThan(2000);
  await expect(page.getByText(`#${token.token_no} `, { exact: false }).first()).toBeVisible();
  const startedService = await staffApi.post(`${API_URL}/v1/tokens/${token.id}/start`, {
    headers: staffAuth,
    data: { version: token.version, service_id: queue.service },
  });
  expect(startedService.status(), await startedService.text()).toBe(200);
  // The service staff chose arrives with the delta, not only with the next snapshot.
  await expect(rows(page).first().getByText("Checkup")).toBeVisible({ timeout: 3000 });
  await expect(rows(page).first().getByText("In service")).toBeVisible();
});

test("pausing stops new joins and says so; resuming opens it again", async () => {
  await page.getByRole("button", { name: "Paused" }).click();
  await expect(page.getByText("No new joins or check-ins; you can keep serving.")).toBeVisible();
  await noAxeViolations(page, "/console/[id] (paused)");
  const refused = await joinOne();
  expect(refused.status).toBe(409);
  expect(refused.code).toBe("QUEUE_CLOSED");
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.getByText("No new joins or check-ins; you can keep serving.")).toBeHidden();
});

test("two stream failures: the console polls ('Live (slow)') and still keeps up", async () => {
  let failed = 0;
  await console_.route("**/v1/queues/*/stream?*", (route) => {
    failed += 1;
    return route.fulfill({ status: 500, body: "{}" });
  });
  await page.reload();
  await expect(pill(page, "polling")).toHaveText("Live (slow)");
  expect(failed).toBe(2); // two failures, then polling; no hot loop
  await noAxeViolations(page, "/console/[id] (polling)");
  // The queue still moves on this screen, by polling alone.
  const joined = await joinOne();
  expect(joined.status).toBe(201);
  await expect(page.getByText(`#${joined.token_no}`)).toBeVisible({ timeout: 15_000 });
  await console_.unroute("**/v1/queues/*/stream?*");
});

test("a real API restart: reconnecting, then live again", async () => {
  test.setTimeout(120_000); // the stream is retried every 60 s
  await page.reload();
  await expect(pill(page, "live")).toBeVisible();
  restartApi();
  await expect(page.locator('[data-pill="reconnecting"], [data-pill="offline"], [data-pill="polling"]')).toBeVisible();
  await noAxeViolations(page, "/console/[id] (reconnecting)");
  await expect(pill(page, "live")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("heading", { name: queue.name })).toBeVisible();
});

test("closing asks first, and is final for the day", async () => {
  await page.getByRole("button", { name: "Closed" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Close the queue for today?")).toBeVisible();
  await noAxeViolations(page, "/console/[id] (close dialog)");
  await page.keyboard.press("Escape"); // Esc keeps it open
  await expect(dialog).toBeHidden();
  await expect(page.getByText("Closed for today; finish the people still waiting.")).toBeHidden();
  await page.getByRole("button", { name: "Closed" }).click();
  await page.getByRole("button", { name: "Yes, close it" }).click();
  await expect(page.getByText("Closed for today; finish the people still waiting.")).toBeVisible();
  await noAxeViolations(page, "/console/[id] (closed)");
});

test("the owner with several businesses picks one", async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(countUntitled);
  const ownerPage = await signIn(context, accounts().owner);
  await ownerPage.goto("/console");
  await expect(ownerPage.getByRole("heading", { name: "Choose a business" })).toBeVisible();
  await expectTitle(ownerPage, "Choose a business · QueueLess");
  const listed = ownerPage.locator("main ul li");
  await expect(listed.first()).toBeVisible(); // count() does not wait for the list to load
  expect(await listed.count()).toBeGreaterThan(1);
  await noAxeViolations(ownerPage, "/console (picker)");
  await ownerPage.getByRole("link", { name: new RegExp(queue.name) }).click();
  await expect(ownerPage).toHaveURL(new RegExp(`/console/${queue.id}$`));
  await expectTitle(ownerPage, "Queue console · QueueLess"); // still titled after navigating
  await context.close();
});

test("a staff member removed by the owner is told their access ended", async () => {
  const removed = await ownerApi.delete(`${API_URL}/v1/businesses/${queue.id}/staff/${staffUserId}`, { headers: ownerAuth });
  expect(removed.status(), await removed.text()).toBe(204);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your access to this business has ended" })).toBeVisible({ timeout: 70_000 });
  await expect(page.locator("main").getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Choose another business" })).toBeVisible();
  await noAxeViolations(page, "/console/[id] (access ended)");
});
