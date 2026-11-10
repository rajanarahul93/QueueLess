import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { otpCodeFor, uniquePhone } from "./local-stack";
import { noAxeViolations } from "./page-checks";

// One aborted refresh must not sign anybody out. A reload (or any navigation) kills the refresh
// the page it replaces had in flight, and a flaky network does the same thing; the session
// itself is still good, because the refresh cookie was never spent. The regression this pins:
// the load-time refresh returning null for a TRANSPORT failure is read as "signed out", so the
// guard sends the customer to the sign-in page while their cookie would still have worked.
test.describe.configure({ mode: "serial" });

let customer: BrowserContext;
let page: Page;

test.beforeAll(async ({ browser }) => {
  customer = await browser.newContext();
  page = await customer.newPage();
  await page.goto("/me");
  const phone = uniquePhone();
  await expect(page).toHaveURL(/\/login\?next=%2Fme$/);
  await page.getByLabel("Mobile number").fill(phone.national);
  const sent = new Date(Date.now() - 1000);
  await page.getByRole("button", { name: "Send code" }).click();
  const code = await otpCodeFor(phone.last4, sent);
  await page.getByLabel("Digit 1").click();
  await page.keyboard.type(code);
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
});

test.afterAll(async () => {
  await customer?.close();
});

test("a reload whose refresh is aborted keeps the customer signed in", async () => {
  // Exactly one refresh dies in transport, the way a reload kills the one it interrupts; every
  // later refresh goes through untouched, so the session is provably still usable.
  let aborted = 0;
  await customer.route("**/v1/auth/refresh", async (route) => {
    if (aborted === 0) {
      aborted += 1;
      await route.abort("failed");
      return;
    }
    await route.continue();
  });
  await page.reload();

  // The cookie was never spent, so the page must come back as My appointments - not the phone
  // sign-in form, and certainly not a sign-in form under a signed-in header.
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
  expect(aborted).toBe(1);
  await expect(page.getByRole("heading", { name: "Sign in with your phone" })).toHaveCount(0);
  await page.unrouteAll({ behavior: "wait" });
});

test("a server it cannot reach at all says so, and Retry recovers", async () => {
  // Every refresh dies: after the backoff ladder the screen says so - and still never offers
  // the sign-in form, because nothing has told us the session is gone.
  await customer.route("**/v1/auth/refresh", (route) => route.abort("failed"));
  await page.reload();
  await expect(page.getByText("We can't reach QueueLess. Check your connection.")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("heading", { name: "Sign in with your phone" })).toHaveCount(0);
  await noAxeViolations(page, "/me (unreachable)");

  await customer.unroute("**/v1/auth/refresh");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "My appointments" })).toBeVisible();
});
