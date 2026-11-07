import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, otpCodeFor, restartApi, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// The staff console in a Los Angeles browser against a business in another zone (Kolkata unless
// that is near midnight; global setup). Customers join through the API; staff act on the screen.
// Two staff members share this queue, so two consoles can race each other. The e2e stack ends
// streams after 20 s (its Compose override) to show reconnects.
test.describe.configure({ mode: "serial" });

type Person = { email: string; password: string };
type Queue = { id: string; name: string; service: string; timezone: string; staff: Person; staff2: Person };
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
/** A row is a button whose name is its whole content, so match how it starts. */
const rowOf = (where: Page, no: number) => where.getByRole("button", { name: new RegExp(`^#${no}(?!\\d)`) });
const said = (where: Page) => where.getByRole("status");

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

/** A fresh customer with a bearer: one OTP request and one verify. */
async function freshCustomer(): Promise<{ api: APIRequestContext; auth: Record<string, string> }> {
  const phone = uniquePhone();
  const api = await request.newContext();
  const sent = new Date(Date.now() - 1000);
  const requested = await api.post(`${API_URL}/v1/auth/otp/request`, { data: { phone: phone.e164 } });
  expect(requested.status()).toBe(202);
  const code = await otpCodeFor(phone.last4, sent); // the mock provider logs it (local only)
  const verified = await api.post(`${API_URL}/v1/auth/otp/verify`, { data: { phone: phone.e164, code } });
  expect(verified.ok(), await verified.text()).toBe(true);
  return { api, auth: { Authorization: `Bearer ${(await verified.json()).access_token}` } };
}

/** A few customers, reused. Each one costs an OTP request and a verify, and the whole job
 *  shares one per-IP budget (20 requests an hour), so this spec borrows from a pool instead of
 *  minting a customer per join. A customer may hold only one active token at a time, which the
 *  rotation respects. */
const POOL_SIZE = 4;
const pool: { api: APIRequestContext; auth: Record<string, string> }[] = [];
let borrowed = 0;

async function borrowCustomer(): Promise<{ api: APIRequestContext; auth: Record<string, string> }> {
  if (pool.length < POOL_SIZE) {
    const made = await freshCustomer();
    pool.push(made);
    return made;
  }
  const pick = pool[borrowed % pool.length];
  borrowed += 1;
  return pick;
}

/** A customer joins this queue through the API; `leave` gives up the token again. */
async function joinOne(): Promise<{ status: number; token_no?: number; code?: string; leave?: () => Promise<number> }> {
  for (let attempt = 0; attempt <= POOL_SIZE; attempt += 1) {
    const customer = await borrowCustomer();
    // A pooled context keeps its connections, and this spec restarts the API, so the first
    // request afterwards can land on a socket the server has already dropped. The retry carries
    // the same Idempotency-Key, so a first attempt the server did accept replays instead of
    // joining twice.
    const headers = { ...customer.auth, "Idempotency-Key": crypto.randomUUID() };
    const post = () => customer.api.post(`${API_URL}/v1/queues/${queueId}/tokens`, { headers, data: {} });
    const joined = await post().catch(() => post());
    const body = await joined.json().catch(() => ({}));
    if (joined.status() !== 409 || body.code !== "ALREADY_IN_QUEUE") {
      const leave = async () => {
        const left = await customer.api.delete(`${API_URL}/v1/tokens/${body.id}`, { headers: customer.auth });
        return left.status();
      };
      return { status: joined.status(), token_no: body.token_no, code: body.code, leave };
    }
    borrowed += 1; // this one still holds a token: try the next
  }
  throw new Error("every pooled customer still holds a token");
}

/** Ends every active token through the API, whatever state it is in, so a test can start from
 *  an empty queue without depending on what the screen managed to do. */
async function emptyQueue(): Promise<void> {
  for (let round = 0; round < 5; round += 1) {
    const listed = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
    const tokens = (await listed.json()).tokens as { id: string; status: string; version: number }[];
    if (tokens.length === 0) return;
    for (const token of tokens) {
      const action = token.status === "in_service" ? "complete" : "no-show";
      const ended = await staffApi.post(`${API_URL}/v1/tokens/${token.id}/${action}`, { headers: staffAuth, data: { version: token.version } });
      expect([200, 409]).toContain(ended.status());
    }
  }
}

/** A booked appointment, checked in: the only way to get an appointment token. */
async function bookAndCheckIn(): Promise<{ token_no: number }> {
  const customer = await borrowCustomer();
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: queue.timezone }).format(new Date());
  const free = await customer.api.get(`${API_URL}/v1/businesses/${queue.id}/availability`, {
    params: { date: day, service_id: queue.service },
    headers: customer.auth,
  });
  expect(free.ok(), await free.text()).toBe(true);
  const slots = (await free.json()).slots as { start: string; resource_id: string }[];
  // Check-in opens 30 minutes before the slot and closes at its end, so take the next one.
  const soon = slots.find((slot) => new Date(slot.start).getTime() > Date.now() - 20 * 60_000);
  expect(soon, `no bookable slot today on ${queue.name}`).toBeTruthy();
  const booked = await customer.api.post(`${API_URL}/v1/appointments`, {
    headers: customer.auth,
    data: { service_id: queue.service, resource_id: soon!.resource_id, start: soon!.start },
  });
  expect(booked.status(), await booked.text()).toBe(201);
  const checked = await customer.api.post(`${API_URL}/v1/appointments/${(await booked.json()).id}/check-in`, { headers: customer.auth });
  expect(checked.status(), await checked.text()).toBe(201);
  return { token_no: (await checked.json()).token_no };
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
  for (const customer of pool) await customer.api.dispose();
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

test("the full flow on screen: Call next, Start with a service, Complete", async () => {
  const joined = await joinOne();
  expect(joined.status).toBe(201);
  const number = joined.token_no!;
  await expect(rowOf(page, number)).toBeVisible();
  await page.getByRole("button", { name: "Call next" }).click();
  await expect(said(page)).toContainText(`Called #${number}`);
  await expect(rowOf(page, number)).toContainText("Called");
  // A walk-in cannot be started until a service is chosen.
  await expect(page.getByRole("button", { name: "Start" })).toBeDisabled();
  await page.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(said(page)).toContainText(`Started #${number}`);
  await expect(rowOf(page, number)).toContainText("Checkup");
  await noAxeViolations(page, "/console/[id] (in service, detail)");
  await page.getByRole("button", { name: "Complete" }).click();
  await expect(said(page)).toContainText(`Completed #${number}`);
  await expect(rowOf(page, number)).toHaveCount(0);
  // The API agrees: the token is completed and its session closed.
  const after = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
  expect(((await after.json()).tokens as { token_no: number }[]).some((item) => item.token_no === number)).toBe(false);
});

test("an appointment token offers no service picker", async () => {
  // A booked appointment, checked in through the API, becomes an appointment token.
  const slot = await bookAndCheckIn();
  await expect(rowOf(page, slot.token_no)).toBeVisible();
  await page.getByRole("button", { name: "Call next" }).click();
  await expect(said(page)).toContainText(`Called #${slot.token_no}`);
  await expect(page.getByLabel("Service for this walk-in")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Start" })).toBeEnabled();
  await page.getByRole("button", { name: "Start" }).click();
  await expect(said(page)).toContainText(`Started #${slot.token_no}`);
  await page.getByRole("button", { name: "Complete" }).click();
  await expect(said(page)).toContainText(`Completed #${slot.token_no}`);
});

test("two consoles calling at once take two different tokens, in order", async ({ browser }) => {
  const first = await joinOne();
  const second = await joinOne();
  expect([first.status, second.status]).toEqual([201, 201]);
  const other = await browser.newContext();
  const otherPage = await signIn(other, queue.staff2);
  await expect(otherPage.getByRole("heading", { name: queue.name })).toBeVisible();
  await Promise.all([
    page.getByRole("button", { name: "Call next" }).click(),
    otherPage.getByRole("button", { name: "Call next" }).click(),
  ]);
  await expect(said(page)).toContainText("Called #");
  await expect(said(otherPage)).toContainText("Called #");
  const listed = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
  const tokens = (await listed.json()).tokens as { id: string; token_no: number; status: string; version: number }[];
  const called = tokens.filter((item) => item.status === "called");
  expect(called.map((item) => item.token_no).sort()).toEqual([first.token_no, second.token_no].sort());
  // Staff B sees what staff A did, live.
  const started = Date.now();
  await page.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await page.getByRole("button", { name: "Start" }).click();
  await expect(otherPage.getByText("In service").first()).toBeVisible({ timeout: 2000 });
  console.log(`console: staff B saw staff A's action in ${Date.now() - started} ms`);
  await other.close();
  // Leave the queue empty for the next test, through the API: whatever each token's state is.
  await emptyQueue();
  await expect(rows(page)).toHaveCount(0, { timeout: 6000 });
});

test("two consoles starting the same token: one wins, the other is told and never retries", async ({ browser }) => {
  const joined = await joinOne();
  const number = joined.token_no!;
  await page.getByRole("button", { name: "Call next" }).click();
  await expect(said(page)).toContainText(`Called #${number}`);
  const other = await browser.newContext();
  const otherPage = await signIn(other, queue.staff2);
  await expect(otherPage.getByRole("heading", { name: queue.name })).toBeVisible();
  await otherPage.getByRole("button", { name: new RegExp(`^#${number}(?!\\d)`) }).click();
  let starts = 0;
  // Both requests are held briefly, so both are sent with the SAME version and really race.
  // Without this, the first response relabels the other console's button before it is pressed.
  for (const where of [page, otherPage]) {
    await where.route("**/v1/tokens/*/start", async (route) => {
      if (where === otherPage) starts += 1;
      await new Promise((resolve) => setTimeout(resolve, 800));
      await route.continue();
    });
  }
  await page.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await otherPage.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await Promise.all([
    page.getByRole("button", { name: "Start" }).click(),
    otherPage.getByRole("button", { name: "Start" }).click(),
  ]);
  // One of the two screens is told it was behind; neither retries on its own.
  const notice = (where: Page) => where.locator("main").getByRole("alert");
  await expect.poll(async () => (await notice(page).count()) + (await notice(otherPage).count()), { timeout: 10_000 }).toBeGreaterThan(0);
  const loser = (await notice(otherPage).count()) > 0 ? otherPage : page;
  await expect(notice(loser)).toContainText("Someone else changed this");
  await noAxeViolations(loser, "/console/[id] (conflict notice)");
  await expect(loser.getByText("In service").first()).toBeVisible(); // the new state is on screen
  expect(starts).toBeLessThanOrEqual(1);
  // A deliberate press on the refreshed state does the right thing.
  const winner = loser === page ? otherPage : page;
  await winner.getByRole("button", { name: "Complete" }).click();
  await expect(said(winner)).toContainText(`Completed #${number}`);
  await other.close();
  await emptyQueue();
});

test("staff B's primary is renamed by staff A's action, and a press inside the hold does nothing", async ({ browser }) => {
  const joined = await joinOne();
  const number = joined.token_no!;
  await page.getByRole("button", { name: "Call next" }).click();
  await expect(said(page)).toContainText(`Called #${number}`);
  const other = await browser.newContext();
  const otherPage = await signIn(other, queue.staff2);
  await expect(otherPage.getByRole("heading", { name: queue.name })).toBeVisible();
  await rowOf(otherPage, number).click(); // B is looking at the same token: its primary is Start
  // A walk-in needs a service before Start is live, so B is ready to press it.
  await otherPage.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await expect(otherPage.getByRole("button", { name: "Start" })).toBeEnabled();
  let presses = 0;
  await otherPage.route("**/v1/tokens/*/complete", async (route) => {
    presses += 1;
    await route.continue();
  });
  // A finger already moving: the page itself presses the primary the instant it is renamed,
  // which is well inside the hold and needs no race against the test runner's own latency.
  await otherPage.evaluate(() => {
    const window_ = window as unknown as { pressedHeld?: boolean };
    const watch = new MutationObserver(() => {
      const button = [...document.querySelectorAll("main button")].find((node) => node.textContent === "Complete");
      if (!button) return;
      watch.disconnect();
      window_.pressedHeld = true;
      (button as HTMLButtonElement).click();
    });
    watch.observe(document.body, { subtree: true, childList: true, characterData: true });
  });
  // A starts it. B's button becomes Complete and is held for a moment.
  await page.getByLabel("Service for this walk-in").selectOption({ label: "Checkup" });
  await page.getByRole("button", { name: "Start" }).click();
  const renamed = otherPage.getByRole("button", { name: "Complete" });
  await expect(renamed).toBeVisible({ timeout: 5000 });
  expect(await otherPage.evaluate(() => (window as unknown as { pressedHeld?: boolean }).pressedHeld)).toBe(true);
  expect(presses).toBe(0); // the press inside the hold sent nothing
  await expect(said(otherPage)).toContainText(`#${number} is now in service`);
  await noAxeViolations(otherPage, "/console/[id] (primary held)");
  // The API agrees: B's press changed nothing.
  const listed = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
  const mine = ((await listed.json()).tokens as { token_no: number; status: string }[]).find((item) => item.token_no === number);
  expect(mine?.status).toBe("in_service");
  // Once the hold is over the same button works.
  await expect(renamed).toBeEnabled({ timeout: 5000 });
  await renamed.click();
  await expect(said(otherPage)).toContainText(`Completed #${number}`);
  await other.close();
  await emptyQueue();
});

test("a customer leaving while staff calls: one wins and both screens agree", async () => {
  const joined = await joinOne();
  const number = joined.token_no!;
  await expect(rowOf(page, number)).toBeVisible();
  const [left] = await Promise.all([
    joined.leave!(),
    page.getByRole("button", { name: "Call next" }).click(),
  ]);
  expect([204, 409]).toContain(left);
  // Whoever won, the row is gone from the screen and from the API.
  await expect(rowOf(page, number)).toHaveCount(0, { timeout: 6000 });
  const listed = await staffApi.get(`${API_URL}/v1/queues/${queueId}`, { headers: staffAuth });
  expect(((await listed.json()).tokens as { token_no: number }[]).some((item) => item.token_no === number)).toBe(false);
});

test("skip twice, then the third press is a labelled No-show with a confirmation", async () => {
  const joined = await joinOne();
  const number = joined.token_no!;
  await page.getByRole("button", { name: "Call next" }).click();
  await expect(said(page)).toContainText(`Called #${number}`);
  await page.getByRole("button", { name: "Skip (2/2)" }).click();
  await expect(said(page)).toContainText(`Skipped #${number}`);
  await page.getByRole("button", { name: "Call next" }).click();
  await page.getByRole("button", { name: "Skip (1/2)" }).click();
  await expect(said(page)).toContainText(`Skipped #${number}`);
  await page.getByRole("button", { name: "Call next" }).click();
  // The next skip would end the token, so it is labelled and confirmed as a no-show.
  await expect(page.getByRole("button", { name: "Skip (1/2)" })).toHaveCount(0);
  await page.getByRole("button", { name: "No-show" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(`Mark #${number} as not here?`);
  await noAxeViolations(page, "/console/[id] (no-show dialog)");
  await page.keyboard.press("Escape");
  await expect(rowOf(page, number)).toBeVisible(); // Esc keeps them
  await page.getByRole("button", { name: "No-show" }).first().click();
  await page.getByRole("button", { name: "Yes, not here" }).click();
  await expect(said(page)).toContainText(`Marked #${number} no-show`);
  await expect(rowOf(page, number)).toHaveCount(0);
});

test("a priority change moves the row", async () => {
  const first = await joinOne();
  const second = await joinOne();
  await expect(rowOf(page, second.token_no!)).toBeVisible();
  const order = async () => (await rows(page).allTextContents()).map((text) => text.slice(0, 6));
  expect((await order())[0]).toContain(`#${first.token_no}`);
  await rowOf(page, second.token_no!).click();
  await page.getByRole("button", { name: "Urgent" }).click();
  await expect(said(page)).toContainText("Priority set");
  await expect.poll(async () => (await order())[0], { timeout: 6000 }).toContain(`#${second.token_no}`);
  await noAxeViolations(page, "/console/[id] (detail panel)");
});

test("pausing stops new joins and says so; resuming opens it again", async () => {
  await page.getByRole("button", { name: "Paused" }).click();
  await expect(page.getByText("No new joins or check-ins; you can keep serving.")).toBeVisible();
  await noAxeViolations(page, "/console/[id] (paused)");
  const refused = await joinOne();
  expect(refused.status).toBe(409);
  expect(refused.code).toBe("QUEUE_CLOSED");
  // Serving carries on while paused: staff can still call whoever is already waiting.
  await expect(page.getByRole("button", { name: "Call next" })).toBeEnabled();
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
  test.setTimeout(90_000); // measured 4.0-20.4 s locally; the bound is RECHECK_SEC + a margin
  const removed = await ownerApi.delete(`${API_URL}/v1/businesses/${queue.id}/staff/${staffUserId}`, { headers: ownerAuth });
  expect(removed.status(), await removed.text()).toBe(204);
  // The open console hears it from the stream, which re-checks membership every RECHECK_SEC
  // (60 s, backend app/realtime/sse.py:34), so the wait is that plus a margin.
  const askedAt = Date.now();
  await expect(page.getByRole("heading", { name: "Your access to this business has ended" })).toBeVisible({ timeout: 75_000 });
  console.log(`console: access ended showed ${((Date.now() - askedAt) / 1000).toFixed(1)} s after the removal`);
  await expect(page.locator("main").getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Choose another business" })).toBeVisible();
  await noAxeViolations(page, "/console/[id] (access ended)");
  // After a reload the member list simply does not have it, which reads neutrally.
  await page.reload();
  await expect(page.getByRole("heading", { name: "You don't have access to this business." })).toBeVisible({ timeout: 20_000 });
  await noAxeViolations(page, "/console/[id] (not yours)");
});
