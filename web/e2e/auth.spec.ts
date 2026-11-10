import { expect, test, type BrowserContext } from "@playwright/test";
import { readFileSync } from "node:fs";
import { ACCOUNTS_FILE } from "./global-setup";
import { otpCodeFor, uniquePhone } from "./local-stack";
import { noAxeViolations } from "./page-checks";

// One serial chain per browser project. Each role signs in once and its later tests reuse that
// live context: refresh tokens rotate, so a saved login reused by several tests would trip the
// reuse detection and revoke the session.
test.describe.configure({ mode: "serial" });

type Accounts = { owner: { email: string; password: string }; staff: { email: string; password: string } };
const accounts = (): Accounts => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));

let customer: BrowserContext;

test("public screens pass axe", async ({ page }) => {
  for (const [path, heading] of [
    ["/", "QueueLess"],
    ["/login", "Sign in with your phone"],
    ["/signin", "Staff and owner sign in"],
    ["/register", "Create an owner account"],
    ["/no-access", "You don't have access to this page"],
  ]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await noAxeViolations(page, path);
  }
});

test("a customer signs in with a phone OTP read from the API log", async ({ browser }) => {
  customer = await browser.newContext();
  const page = await customer.newPage();
  const phone = uniquePhone();
  await page.goto("/login");
  await page.getByLabel("Mobile number").fill(phone.national);
  const sent = new Date(Date.now() - 1000);
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByRole("group", { name: "Verification code" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Resend in \d+ s/ })).toBeDisabled();
  await noAxeViolations(page, "/login (code step)");
  const code = await otpCodeFor(phone.last4, sent);
  await page.getByLabel("Digit 1").click();
  await page.keyboard.type(code); // the sixth digit submits
  await expect(page).toHaveURL(/\/me$/);
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
  await noAxeViolations(page, "/me");
});

test("a reload keeps the session (silent refresh from the cookie)", async () => {
  const page = customer.pages()[0];
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toMatch(/eyJ/); // no JWT in web storage
});

test("two tabs refreshing at once stay signed in (no reuse, no logout)", async () => {
  const first = customer.pages()[0];
  const second = await customer.newPage();
  // Hold each refresh 300 ms so both tabs are refreshing at the same moment: without the
  // cross-tab lock both would send the same refresh cookie, and the second use revokes all.
  await customer.route("**/v1/auth/refresh", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.continue();
  });
  const refreshes: string[] = [];
  for (const page of [first, second]) {
    page.on("response", (response) => {
      if (response.url().endsWith("/v1/auth/refresh")) refreshes.push(String(response.status()));
    });
  }
  await Promise.all([first.goto("/me"), second.goto("/me")]);
  for (const page of [first, second]) {
    await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
  }
  expect(refreshes.length).toBeGreaterThan(0);
  expect(refreshes.every((status) => status === "200")).toBe(true);
  await customer.unrouteAll({ behavior: "wait" }); // let a held refresh finish first
  await first.reload(); // the cookie the two tabs left behind still works
  await expect(first.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
});

test("a customer opening an owner or staff route is blocked", async () => {
  const page = customer.pages()[0];
  for (const path of ["/owner", "/console"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: "You don't have access to this page" })).toBeVisible();
    await expect(page.getByText("Signed in as")).toHaveCount(0);
  }
});

test("signing out in one tab signs out the other", async () => {
  const [first, second] = customer.pages();
  await first.goto("/me");
  await second.goto("/me");
  await expect(second.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
  await first.getByRole("button", { name: "Sign out" }).click();
  await expect(second).toHaveURL(/\/login\?next=%2Fme$/);
  await second.reload();
  await expect(second).toHaveURL(/\/login/); // the server revoked the refresh family too
  await customer.close();
});

test("staff and owner sign in with email and land on their screens", async ({ browser }) => {
  // The staff member of this run works at one business, so the picker opens its console
  // straight away (6D-1); the owner still lands on their own screen.
  for (const [role, home, heading] of [
    ["staff", /\/console\/[0-9a-f-]{36}$/, "E2E Clinic"],
    // 6E-1 replaced the owner placeholder with the hub, which names itself.
    ["owner", /\/owner$/, "Your business"],
  ] as const) {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(`/signin`);
    await page.getByLabel("Email").fill(accounts()[role].email);
    await page.getByLabel("Password").fill(accounts()[role].password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(home);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await noAxeViolations(page, `${role} home`);
    await context.close();
  }
});

test("a wrong password shows the copy, not the server's words", async ({ page }) => {
  await page.goto("/signin");
  // an unknown email: same answer as a wrong password, and no account's per-email budget used
  await page.getByLabel("Email").fill(`nobody-${Date.now()}@example.test`);
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  // (Next's route announcer is also role="alert", and empty)
  await expect(page.getByRole("alert").filter({ hasText: /\S/ })).toHaveText(
    "Your email or password isn't right.",
  );
  await noAxeViolations(page, "/signin (error)");
});

test("an owner registers and is signed in", async ({ page }) => {
  await page.goto("/register");
  await page.getByLabel("Your name").fill("New Owner");
  await page.getByLabel("Email").fill(`e2e-new-${Date.now()}@example.test`);
  await page.getByLabel(/Password/).fill("a-long-password-1");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/owner$/);
  // 6E-1 replaced the owner placeholder, which greeted the new owner by name, with the hub:
  // a brand-new owner has nothing yet, so the empty state is the proof the guarded page loaded.
  await expect(page.getByRole("heading", { level: 1, name: "Your business" })).toBeVisible();
  await expect(page.getByText("You have no business yet.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
});
