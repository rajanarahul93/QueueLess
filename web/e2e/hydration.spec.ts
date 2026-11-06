import { expect, test } from "@playwright/test";

// The server HTML of every form must be inert until React hydrates it: typing into a field
// React does not control yet is lost, and a click or Enter would submit the form natively.
// The app's JavaScript is blocked here, so these pages render but never hydrate.
const FORMS = [
  { path: "/login", submit: "Send code" },
  { path: "/signin", submit: "Sign in" },
  { path: "/register", submit: "Create account" },
  { path: "/b", submit: "Search" },
];

for (const { path, submit } of FORMS) {
  test(`${path}: the form does nothing before the page is interactive`, async ({ page }) => {
    await page.route("**/_next/static/chunks/*.js", (route) => route.abort());
    const sent: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "document" || request.url().includes("/v1/")) sent.push(request.url());
    });
    await page.goto(path);
    const loaded = page.url();
    sent.length = 0;

    const form = page.locator("main form");
    const fields = form.locator("input, select, textarea");
    const button = form.getByRole("button", { name: submit });
    await button.click({ force: true }); // a real click; a disabled button ignores it
    await fields.first().press("Enter"); // implicit submission, if the field could take focus
    await page.waitForTimeout(300);

    expect(page.url()).toBe(loaded); // no native GET or POST navigation
    expect(sent).toEqual([]); // no document reload, no API call
    await expect(button).toBeDisabled();
    for (const field of await fields.all()) await expect(field).toBeDisabled();

    // The same form in a fresh tab without the block hydrates and is live (a reload of the
    // blocked tab keeps the aborted scripts in WebKit).
    const fresh = await page.context().newPage();
    await fresh.goto(path);
    const live = fresh.locator("main form");
    await expect(live.getByRole("button", { name: submit })).toBeEnabled();
    for (const field of await live.locator("input, select, textarea").all()) await expect(field).toBeEnabled();
  });
}

test("/b: after hydration the search works as usual", async ({ page }) => {
  await page.goto("/b");
  await page.getByLabel("Name").fill("zzz no such business");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/\/b\?q=zzz/);
  await expect(page.getByText("No businesses match your search.")).toBeVisible();
});

// Protected pages render only a skeleton on the server: nothing to press before hydration.
for (const path of ["/me", "/ticket", "/ticket/00000000-0000-0000-0000-000000000000", "/console", "/console/00000000-0000-0000-0000-000000000000"]) {
  test(`${path}: no enabled control before the page is interactive`, async ({ page }) => {
    await page.route("**/_next/static/chunks/*.js", (route) => route.abort());
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    await expect(page.locator("main").locator("button:enabled, input:enabled, select:enabled, textarea:enabled, a[href]")).toHaveCount(0);
  });
}

