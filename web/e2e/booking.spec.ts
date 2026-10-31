import AxeBuilder from "@axe-core/playwright";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { addDays, dayButtonName, dayLabel, localToday } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, otpCodeFor, restrictBooking, uniquePhone } from "./local-stack";

// Browse and book against the Kolkata booking business, in a Los Angeles browser (config).
// Two customers per browser project (2 OTP sign-ins); each project books its own day.
test.describe.configure({ mode: "serial" });

type Booking = { id: string; name: string; service: string };
const booking = (): Booking => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8")).booking;

async function noAxeViolations(page: Page, screen: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations.map((violation) => `${screen}: ${violation.id}`)).toEqual([]);
}

/** Signs in on the login page the app sent us to; returns the phone used. */
async function signInWithOtp(page: Page): Promise<string> {
  const phone = uniquePhone();
  await expect(page.getByRole("heading", { name: "Sign in with your phone" })).toBeVisible();
  await page.getByLabel("Mobile number").fill(phone.national);
  const sent = new Date(Date.now() - 1000);
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByRole("group", { name: "Verification code" })).toBeVisible();
  const code = await otpCodeFor(phone.last4, sent);
  await page.getByLabel("Digit 1").click();
  await page.keyboard.type(code);
  return phone.e164;
}

let day: string; // this project's booking day, business-local
let a: BrowserContext;
let b: BrowserContext;
let phoneB: string;

/** Counts the moments the loaded page had no title (a remounted <title> leaves the tab untitled). */
function countUntitled() {
  addEventListener("DOMContentLoaded", () => {
    const counter = window as unknown as { untitled: number };
    counter.untitled = 0;
    new MutationObserver(() => {
      if (!document.title) counter.untitled += 1;
    }).observe(document.head, { subtree: true, childList: true, characterData: true });
  });
}

/** After a client-side navigation the Book page still has its own title, and never lost it. */
async function expectBookTitle(page: Page) {
  await expect(page).toHaveTitle(`Book at ${booking().name} · QueueLess`);
  expect(await page.evaluate(() => (window as unknown as { untitled: number }).untitled)).toBe(0);
}

async function pickDay(page: Page) {
  await page.getByRole("button", { name: dayButtonName(day), exact: true }).click();
  await expect(page.getByRole("button", { name: dayButtonName(day), exact: true })).toHaveAttribute("aria-pressed", "true");
  await expectBookTitle(page); // the day is in the URL: picking it is a router.replace
}

/** Picks a time the way a person does: by clicking its visible label (the radio is hidden). */
async function pickTime(page: Page, time: string) {
  await page.getByRole("radiogroup", { name: "Available times" }).getByText(time, { exact: true }).click();
  await expect(page.getByRole("radio", { name: time })).toBeChecked();
}

/** The error shown where Confirm is (Next's route announcer is also role="alert"). */
function confirmAlert(page: Page) {
  return page.getByRole("region", { name: "Confirm booking" }).getByRole("alert");
}

async function bookAt(page: Page, time: string) {
  await pickTime(page, time);
  await page.getByRole("button", { name: "Confirm booking" }).click();
}

test.beforeAll(({}, testInfo) => {
  const index = ["chromium", "firefox", "webkit"].indexOf(testInfo.project.name);
  day = addDays(localToday("Asia/Kolkata"), index + 1);
});

test("search finds the business and opens it (signed out)", async ({ page }) => {
  await page.goto(`/b?q=${encodeURIComponent("zzz no such business")}`);
  await expect(page.getByText("No businesses match your search.")).toBeVisible();
  await expect(page).toHaveTitle("Find a business · QueueLess");
  await noAxeViolations(page, "/b (empty)");
  await page.getByLabel("Name").fill(booking().name);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page).toHaveURL(/\/b\?q=/);
  await page.getByRole("link", { name: new RegExp(booking().name) }).click();
  await expect(page.getByRole("heading", { level: 1, name: booking().name })).toBeVisible();
  await expect(page.getByText("Times are local to the business (Asia/Kolkata).")).toBeVisible();
  await expect(page).toHaveTitle(`${booking().name} · QueueLess`);
  await noAxeViolations(page, "/b/[id]");
  await page.goto(`/b?q=${encodeURIComponent(booking().name)}`);
  await expect(page.getByRole("link", { name: new RegExp(booking().name) })).toBeVisible();
  await noAxeViolations(page, "/b (results)");
});

test("Book asks for sign-in and comes back to the same service", async ({ browser }) => {
  a = await browser.newContext();
  await a.addInitScript(countUntitled);
  const page = await a.newPage();
  await page.goto(`/b/${booking().id}`);
  await page.getByRole("link", { name: "Book Checkup" }).click();
  await expect(page).toHaveURL(/\/login\?next=/);
  await signInWithOtp(page);
  await expect(page).toHaveURL(new RegExp(`/b/${booking().id}/book\\?service=${booking().service}`));
  await expect(page.getByRole("heading", { name: `Book at ${booking().name}` })).toBeVisible();
  await expectBookTitle(page);
});

test("a slot is booked and shown in the business's time, not the browser's", async () => {
  const page = a.pages()[0];
  await pickDay(page);
  // 09:00 in Kolkata is 19:30 the day before in Los Angeles; the label must say 9:00 AM
  await expect(page.getByRole("radio", { name: "9:00 AM" })).toBeVisible();
  await expect(page.getByText("30 min · times shown in Asia/Kolkata")).toBeVisible();
  await noAxeViolations(page, "/b/[id]/book (slots)");
  await bookAt(page, "9:00 AM");
  await expect(page.getByRole("heading", { name: "You're booked" })).toBeVisible();
  await expect(page.getByText(`Checkup on ${dayLabel(day, "long")} at 9:00 AM`)).toBeVisible();
  await noAxeViolations(page, "/b/[id]/book (booked)");
});

test("two customers race for one slot: the second is offered the next ones and books", async ({ browser }) => {
  b = await browser.newContext();
  await b.addInitScript(countUntitled);
  const pageB = await b.newPage();
  await pageB.goto(`/b/${booking().id}/book?service=${booking().service}&date=${day}`);
  phoneB = await signInWithOtp(pageB);
  await expect(pageB.getByRole("radio", { name: "9:30 AM" })).toBeVisible();
  await pickTime(pageB, "9:30 AM"); // B is on the confirm step

  const pageA = a.pages()[0];
  await pageA.getByRole("button", { name: "Book another time" }).click();
  await pickDay(pageA);
  await bookAt(pageA, "9:30 AM"); // A takes it first
  await expect(pageA.getByRole("heading", { name: "You're booked" })).toBeVisible();

  await pageB.getByRole("button", { name: "Confirm booking" }).click();
  const alert = pageB.getByRole("alert").filter({ hasText: "That time was just taken" });
  await expect(alert).toBeVisible();
  await expect(alert).toBeFocused();
  await expect(pageB).toHaveURL(new RegExp(`date=${day}`)); // service and date kept
  await noAxeViolations(pageB, "/b/[id]/book (SLOT_TAKEN)");
  await alert.getByRole("button", { name: "10:00 AM" }).click();
  await pageB.getByRole("button", { name: "Confirm booking" }).click();
  await expect(pageB.getByText(`Checkup on ${dayLabel(day, "long")} at 10:00 AM`)).toBeVisible();
});

/** The customer's booked appointments starting at this business-local time, from the API. The
 *  context's own refresh cookie gives a token (the page keeps its token in memory only). */
async function bookedAt(context: BrowserContext, time: string): Promise<number> {
  const refreshed = await context.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok()).toBe(true);
  const { access_token: bearer } = await refreshed.json();
  const list = await context.request.get(`${API_URL}/v1/appointments?status=booked&limit=50`, {
    headers: { Authorization: `Bearer ${bearer}` },
  });
  const { items } = (await list.json()) as { items: { start: string }[] };
  return items.filter((item) => item.start === `${day}T${time}:00+05:30`).length;
}

for (const [how, time, label] of [
  ["a double click", "10:30", "10:30 AM"],
  ["Enter then a click", "11:00", "11:00 AM"],
] as const) {
  test(`${how} on Confirm books once: one request, one appointment, one success`, async () => {
    const page = a.pages()[0];
    await page.getByRole("button", { name: "Book another time" }).click();
    await pickDay(page);
    await pickTime(page, label);
    // Hold the booking 500 ms (a slow network), so the second press lands while it is in flight.
    await page.route("**/v1/appointments", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 500));
      await route.continue();
    });
    const posts: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/v1/appointments")) posts.push(request.url());
    });
    // By position, not label: while busy the button reads "Please wait…"
    const confirm = page.getByRole("region", { name: "Confirm booking" }).getByRole("button");
    if (how === "a double click") await confirm.dblclick();
    else {
      await confirm.focus();
      await page.keyboard.press("Enter");
      await confirm.click({ force: true }); // the user clicks too, even if the button just went busy
    }
    await expect(page.getByRole("heading", { name: "You're booked" })).toHaveCount(1);
    await page.unrouteAll({ behavior: "wait" });
    expect(posts).toHaveLength(1);
    expect(await bookedAt(a, time)).toBe(1);
  });
}

test("the sixth upcoming booking is refused with the limit message", async () => {
  const page = a.pages()[0];
  for (const time of ["11:30 AM"]) {
    await page.getByRole("button", { name: "Book another time" }).click();
    await pickDay(page);
    await bookAt(page, time);
    await expect(page.getByRole("heading", { name: "You're booked" })).toBeVisible();
  }
  await page.getByRole("button", { name: "Book another time" }).click();
  await pickDay(page);
  await bookAt(page, "12:00 PM");
  await expect(confirmAlert(page)).toHaveText(
    "You already have 5 upcoming appointments. Cancel one to book another.",
  );
  await noAxeViolations(page, "/b/[id]/book (limit)");
  await a.close();
});

test("a customer with three no-shows cannot book", async () => {
  restrictBooking(phoneB);
  const page = b.pages()[0];
  await page.getByRole("button", { name: "Book another time" }).click();
  await pickDay(page);
  await bookAt(page, "12:30 PM");
  await expect(confirmAlert(page)).toContainText(
    "Booking is paused on your account after missed appointments.",
  );
  await b.close();
});
