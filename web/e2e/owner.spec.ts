import { expect, request, test, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

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
async function listed(): Promise<{ id: string; name: string; timezone: string; role: string }[]> {
  const refreshed = await owner.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok()).toBe(true);
  const headers = { Authorization: `Bearer ${(await refreshed.json()).access_token}` };
  const mine = await api.get(`${API_URL}/v1/businesses/mine`, { headers });
  expect(mine.ok(), await mine.text()).toBe(true);
  return mine.json();
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

test("a signed-out visitor is sent to sign in", async ({ browser }) => {
  const stranger = await browser.newContext();
  const blank = await stranger.newPage();
  await blank.goto("/owner");
  await expect(blank).toHaveURL(/\/signin\?next=%2Fowner/);
  await stranger.close();
});
