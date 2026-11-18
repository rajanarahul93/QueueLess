import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, insertDailyStats } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";
import { addDays, localToday } from "../lib/time";

// The owner hub and the create-business form, in a Los Angeles browser. This spec uses the
// SECOND owner from global setup, who has no business of their own, so the empty hub is real
// and the business it creates belongs to nobody else's spec. One sign-in for the whole file.
// The staff and customer refusals live in appointments.console.spec.ts and appointments.spec.ts,
// whose sessions are already signed in: each refusal here would cost one of the 20 logins an IP
// gets every 15 minutes, and a job is at that cap.
test.describe.configure({ mode: "serial" });

type Person = { email: string; password: string };
const accounts = (): { fresh: Person } => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));

let owner: BrowserContext;
let page: Page;
let api: APIRequestContext;
let made: { id: string; name: string } | null = null;

const NAME = `E2E Owner Hub ${Date.now()}`;
/** Read from the page: ICU builds disagree on zone names (Asia/Calcutta vs Asia/Kolkata), so
 *  the test picks one the browser really offers instead of naming one. */
let zone: string;

async function signIn(context: BrowserContext, who: Person, landing: RegExp): Promise<Page> {
  const signed = await context.newPage();
  await signed.goto("/signin");
  await signed.getByLabel("Email").fill(who.email);
  await signed.getByLabel("Password").fill(who.password);
  await signed.getByRole("button", { name: "Sign in" }).click();
  await expect(signed).toHaveURL(landing);
  return signed;
}

/** What the API says this owner has, so the screen is never the only witness. */
async function authHeaders(): Promise<{ Authorization: string }> {
  const refreshed = await owner.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok(), "the owner session still refreshes").toBe(true);
  return { Authorization: `Bearer ${(await refreshed.json()).access_token}` };
}

async function listed(): Promise<{ id: string; name: string; timezone: string; role: string }[]> {
  const headers = await authHeaders();
  const mine = await api.get(`${API_URL}/v1/businesses/mine`, { headers });
  expect(mine.ok(), await mine.text()).toBe(true);
  return mine.json();
}

/** Every test after the create test needs the business it made. The file is serial on purpose
 *  (one sign-in for the whole file, see the note above), so a single test run alone with --grep
 *  has no business to work on: this says exactly that instead of failing on a null id. */
function business(): { id: string; name: string } {
  expect(
    made,
    "this test needs the business created earlier in this serial file: run the whole file, not one test alone",
  ).not.toBeNull();
  return made!;
}

test.beforeAll(async ({ browser }) => {
  api = await request.newContext();
  owner = await browser.newContext();
  await owner.addInitScript(countUntitled);
  page = await signIn(owner, accounts().fresh, /\/owner/);
});

test.afterAll(async () => {
  await owner?.close();
  await api?.dispose();
});

test("an owner with no business sees the empty hub", async () => {
  await expect(page.getByRole("heading", { level: 1, name: "Your business" })).toBeVisible();
  await expect(page.getByText("You have no business yet.")).toBeVisible();
  await expectTitle(page, "Your business · QueueLess");
  await noAxeViolations(page, "/owner (empty)");
  expect(await listed()).toHaveLength(0);
});

test("the form refuses a week that cannot work, and creates nothing", async () => {
  await page.getByRole("button", { name: "Create business" }).click();
  await expect(page).toHaveURL(/\/owner\/new$/);
  await expectTitle(page, "Create a business · QueueLess");
  await noAxeViolations(page, "/owner/new (form)");
  await page.getByLabel("Business name").fill(NAME);
  await page.getByLabel("Category").fill("clinic");
  for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
    await page.getByRole("button", { name: `Close ${day} all day` }).click();
  }
  await page.getByRole("button", { name: "Create business" }).click();
  await expect(page.getByText("Open on at least one day, so people can book.")).toBeVisible();
  await noAxeViolations(page, "/owner/new (hours error)");
  expect(await listed()).toHaveLength(0);
});

test("a double press creates exactly one business", async () => {
  // Back to a week that works, in the business's own zone, not the browser's.
  await page.getByRole("button", { name: "Open Monday" }).click();
  const picker = page.getByLabel("Time zone");
  zone = (await picker.locator("option").evaluateAll((options) =>
    (options as HTMLOptionElement[]).map((option) => option.value).find((value) => value.startsWith("Asia/")),
  )) as string;
  expect(zone, "the browser offers an Asia zone").toBeTruthy();
  await picker.selectOption(zone);
  await expect(page.getByText(/It is .* there now\./)).toBeVisible();
  await page.getByRole("button", { name: "Create business" }).dblclick();
  await expect(page).toHaveURL(/\/owner\?created=/);
  const rows = await listed();
  expect(rows).toHaveLength(1); // one press, one business
  expect(rows[0].timezone).toBe(zone); // the chosen zone, not the browser's
  expect(rows[0].role).toBe("owner");
  made = { id: rows[0].id, name: rows[0].name };
  expect(made.name).toBe(NAME);
});

test("the hub lists it, says so, and links to the screens that exist", async () => {
  await expect(page.getByRole("status")).toHaveText(`Created ${NAME}`);
  const row = page.getByRole("listitem").filter({ hasText: NAME });
  await expect(row).toBeVisible();
  await expect(row.getByText(zone)).toBeVisible();
  await noAxeViolations(page, "/owner (one business)");
  await row.getByRole("link", { name: "Queue console" }).click();
  await expect(page.getByRole("heading", { level: 1, name: NAME })).toBeVisible();
  await page.goBack();
  await page.getByRole("listitem").filter({ hasText: NAME }).getByRole("link", { name: "Appointments today" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Appointments today" })).toBeVisible();
});

// 6E-2: the services of the business this spec just created, in the same signed-in context -
// no extra sign-in of any kind. The spec is serial, so it runs after the hub tests above.
test("the owner adds a service, edits it and stops offering it", async () => {
  business(); // the precondition, before anything on the page
  await page.goto("/owner");
  await page.getByRole("listitem").filter({ hasText: NAME }).getByRole("link", { name: "Services" }).click();
  await expect(page).toHaveURL(new RegExp(`/owner/${business().id}/services$`));
  await expectTitle(page, "Services · QueueLess");
  await expect(page.getByRole("heading", { level: 1, name: "Services" })).toBeVisible();
  await expect(
    page.getByText("No services yet. Add the first one people can book."),
    "this test starts from a business with no service yet",
  ).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/services (empty)");

  // Added with the keyboard only: tab to Add, fill, Enter submits the form.
  await page.getByRole("button", { name: "Add a service" }).click();
  await page.getByLabel("Name").fill("Checkup");
  await page.getByLabel("Duration (minutes)").fill("30");
  await page.getByLabel("Price").fill("250.00");
  await page.getByLabel("Price").press("Enter");
  await expect(page.getByRole("status")).toHaveText("Added Checkup");
  const row = page.getByRole("listitem").filter({ hasText: "Checkup" });
  await expect(row.getByText("30 min · 250.00")).toBeVisible();
  await expect(row.getByText("Offered")).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/services (one service)");

  // An edit sends only what changed; the screen shows what the server has afterwards.
  await row.getByRole("button", { name: "Edit Checkup" }).click();
  await page.getByLabel("Duration (minutes)").fill("45");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved Checkup");
  await expect(page.getByRole("listitem").filter({ hasText: "Checkup" }).getByText("45 min · 250.00")).toBeVisible();

  // Esc keeps it, then the confirmation stops it.
  const stop = page.getByRole("listitem").filter({ hasText: "Checkup" }).getByRole("button", { name: "Stop offering Checkup" });
  await stop.click();
  const dialog = page.getByRole("dialog", { name: "Stop offering this service?" });
  await expect(dialog).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/services (confirm)");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(stop).toBeFocused();
  await expect(page.getByRole("listitem").filter({ hasText: "Checkup" }).getByText("Offered")).toBeVisible();

  await stop.click();
  await dialog.getByRole("button", { name: "Stop offering it" }).click();
  await expect(page.getByRole("status")).toHaveText("Stopped offering Checkup");
  await expect(page.getByRole("listitem").filter({ hasText: "Checkup" }).getByText("Not offered")).toBeVisible();

  // It survives a reload, because nothing here was optimistic.
  await page.reload();
  await expect(page.getByRole("listitem").filter({ hasText: "Checkup" }).getByText("Not offered")).toBeVisible();
  await expect(page.getByRole("button", { name: "Offer Checkup again" })).toBeVisible();
});

test("a service that is not offered is not on the public page", async () => {
  business(); // the precondition, before anything on the page
  // The same context, no sign-in: the public business page shows only what can be booked.
  const guest = await page.context().newPage();
  await guest.goto(`/b/${business().id}`);
  await expect(guest.getByRole("heading", { level: 1, name: NAME })).toBeVisible();
  await expect(guest.getByText("Checkup")).toHaveCount(0);
  await guest.close();
});

// 6E-3: the owner adds a staff member, that person signs in ONCE, and the owner removes them.
// A refused sign-in afterwards would be a second login, and a job already spends 19 of the 20
// an IP gets every 15 minutes, so the removal is proven by what the person's own session sees.
test("the owner adds a staff member, who signs in, and then removes them", async ({ browser }) => {
  business(); // the precondition, before anything on the page
  await page.goto("/owner");
  await page.getByRole("listitem").filter({ hasText: NAME }).getByRole("link", { name: "Staff" }).click();
  await expect(page).toHaveURL(new RegExp(`/owner/${business().id}/staff$`));
  await expectTitle(page, "Staff · QueueLess");
  await expect(page.getByRole("heading", { level: 1, name: "Staff" })).toBeVisible();
  await expect(
    page.getByText("Nobody yet. Add the first person who will run the queue."),
    "this test starts from a business with nobody on the staff yet",
  ).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/staff (empty)");

  const hired = { email: `e2e-hired-${Date.now()}@example.test`, entry: crypto.randomUUID() };
  await page.getByRole("button", { name: "Add a staff member" }).click();
  await page.getByLabel("Name").fill("E2E Hired Staff");
  await page.getByLabel("Email").fill(hired.email);
  await page.getByLabel("First password").fill(hired.entry);
  await expect(page.getByLabel("First password")).toHaveAttribute("type", "password");
  await expect(page.getByLabel("First password")).toHaveAttribute("autocomplete", "new-password");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Added E2E Hired Staff");
  const row = page.getByRole("listitem").filter({ hasText: "E2E Hired Staff" });
  await expect(row.getByText(hired.email)).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/staff (one person)");
  // The first password is nowhere on the page, and the form no longer holds it.
  await expect(page.getByText(hired.entry)).toHaveCount(0);
  await page.getByRole("button", { name: "Add a staff member" }).click();
  await expect(page.getByLabel("First password")).toHaveValue("");
  await page.getByRole("button", { name: "Cancel" }).click();

  // The one new sign-in this spec spends: the person the owner just created.
  const hiredContext = await browser.newContext();
  const hiredPage = await signIn(hiredContext, { email: hired.email, password: hired.entry }, /\/console/);
  await expect(hiredPage.getByRole("heading", { level: 1, name: NAME })).toBeVisible();

  await page.getByRole("button", { name: "Remove E2E Hired Staff" }).click();
  const dialog = page.getByRole("dialog", { name: "Remove this person?" });
  await expect(dialog).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/staff (confirm)");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: "Remove E2E Hired Staff" })).toBeFocused();
  await page.getByRole("button", { name: "Remove E2E Hired Staff" }).click();
  await dialog.getByRole("button", { name: "Remove them" }).click();
  await expect(page.getByRole("status")).toHaveText("Removed E2E Hired Staff");
  await expect(page.getByText("Nobody yet. Add the first person who will run the queue.")).toBeVisible();

  // Their own session has lost the business: no second sign-in needed to prove the removal.
  await hiredPage.goto("/console");
  await expect(hiredPage.getByText("You are not a member of any business yet. Ask an owner to add you.")).toBeVisible();
  await hiredContext.close();
});

// 6E-4: the same owner session, no new sign-in.
test("the owner edits the settings, and they survive a reload", async () => {
  business(); // the precondition, before anything on the page
  await page.goto(`/owner/${business().id}/settings`);
  await expectTitle(page, "Settings · QueueLess");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await expect(page.getByLabel("Business name")).toHaveValue(NAME);
  await noAxeViolations(page, "/owner/[id]/settings");

  // A number outside the server's range is refused before anything is sent.
  await page.getByLabel("Grace period before a no-show (minutes)").fill("61");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("A whole number from 0 to 60.")).toBeVisible();

  await page.getByLabel("Grace period before a no-show (minutes)").fill("7");
  await page.getByLabel("Address (optional)").fill("9 Demo Street");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved");

  await page.reload();
  await expect(page.getByLabel("Grace period before a no-show (minutes)")).toHaveValue("7");
  await expect(page.getByLabel("Address (optional)")).toHaveValue("9 Demo Street");

  // Hours that cannot work are refused too, and nothing is saved. Whatever the week looks
  // like when this test arrives, close every day that is open, so the refusal is the point
  // and not the starting shape of the week.
  const closers = page.getByRole("button", { name: /^Close .+ all day$/ });
  await expect(closers, "the business is open on at least one day to start with").not.toHaveCount(0);
  for (let open = await closers.count(); open > 0; open = await closers.count()) {
    await closers.first().click();
  }
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Open on at least one day, so people can book.")).toBeVisible();
});

// 6E-5: the dashboard reads finished days only, so the days are seeded the way the other specs
// seed rows - by SQL, through the helper in local-stack.ts. Still the same owner session.
test("the owner sees the dashboard, built from finished days only", async () => {
  business(); // the precondition, before anything on the page
  const zone = "Asia/Kolkata";
  const yesterday = addDays(localToday(zone), -1);
  const days = [0, 1, 2].map((back) => addDays(yesterday, -back));
  // include_inactive, because the services spec above stopped offering the only one. That view
  // is owner-only, so it needs the bearer token, not just the session cookie.
  const listing = await api.get(`${API_URL}/v1/businesses/${business().id}/services?include_inactive=true`, {
    headers: await authHeaders(),
  });
  expect(listing.ok(), await listing.text()).toBe(true);
  const services = (await listing.json()) as { id: string }[];
  expect(services.length, "the business has a service to attribute the days to").toBeGreaterThan(0);
  expect(insertDailyStats(business().id, services[0].id, days)).toBe(days.length * 2);

  await page.goto(`/owner/${business().id}/dashboard`);
  await expectTitle(page, "Dashboard · QueueLess");
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  // The range never includes today: the rollup only writes days that are over.
  await expect(page.getByText(`Up to yesterday · ${addDays(yesterday, -29)} to ${yesterday}`)).toBeVisible();

  // The same words head the tables below, so these look at the three summary tiles
  // themselves - the tables are not list items.
  for (const tile of ["Busiest hour", "Average wait", "No-shows"]) {
    await expect(page.getByRole("listitem").filter({ hasText: tile })).toBeVisible();
  }
  await expect(page.getByRole("heading", { level: 2, name: "When people join" })).toBeVisible();

  // Every chart has the same numbers as a table, which is what a screen reader gets.
  await page.getByText("View data").first().click();
  await expect(page.getByRole("table", { name: "Tokens joined" })).toBeVisible();
  await expect(page.getByRole("table", { name: "By service over the range" })).toBeVisible();
  await noAxeViolations(page, "/owner/[id]/dashboard");

  await page.setViewportSize({ width: 360, height: 780 });
  await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
  const scrolls = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(scrolls, "no sideways scrolling at 360 px").toBe(false);
  await page.setViewportSize({ width: 1280, height: 900 });
});

test("a signed-out visitor is sent to sign in", async ({ browser }) => {
  const stranger = await browser.newContext();
  const blank = await stranger.newPage();
  await blank.goto("/owner");
  await expect(blank).toHaveURL(/\/signin\?next=%2Fowner/);
  await stranger.close();
});
