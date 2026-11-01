import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { addDays, dayButtonName, dayLabel, localToday } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, insertAppointments, otpCodeFor, uniquePhone } from "./local-stack";
import { countUntitled, expectTitle, noAxeViolations } from "./page-checks";

// My appointments, cancel and move, in a Los Angeles browser against Kolkata businesses (config).
// One customer per browser project (1 OTP sign-in), appointments seeded through the API. Each
// project uses its own days (today + 4..6) and its own offsets for rows written to the database.
test.describe.configure({ mode: "serial" });

type Place = { id: string; name: string; service: string };
const accounts = (): { booking: Place; cutoff: Place } => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));
const MINUTE = 60_000;

let index: number;
let day: string;
let customer: BrowserContext;
const ids: Record<string, string> = {};

test.beforeAll(({}, testInfo) => {
  index = ["chromium", "firefox", "webkit"].indexOf(testInfo.project.name);
  day = addDays(localToday("Asia/Kolkata"), index + 4);
});

const label = (time: string) => `Checkup on ${dayLabel(day, "long")} at ${time}`;
const kolkata = (time: string) => `${day}T${time}:00+05:30`;

/** A bearer for this context's customer, from its refresh cookie (the page keeps its own). */
async function bearer(context: BrowserContext): Promise<Record<string, string>> {
  const refreshed = await context.request.post(`${API_URL}/v1/auth/refresh`);
  expect(refreshed.ok()).toBe(true);
  return { Authorization: `Bearer ${(await refreshed.json()).access_token}` };
}

/** Books `time` on this project's day through the API; returns the appointment id. */
async function seed(context: BrowserContext, place: Place, time: string): Promise<string> {
  const headers = await bearer(context);
  const free = await context.request.get(
    `${API_URL}/v1/businesses/${place.id}/availability?service_id=${place.service}&date=${day}`,
    { headers },
  );
  const slot = (await free.json()).slots.find((item: { start: string }) => item.start === kolkata(time));
  const booked = await context.request.post(`${API_URL}/v1/appointments`, {
    headers, // no Idempotency-Key: a seed is sent once
    data: { service_id: place.service, resource_id: slot.resource_id, start: slot.start },
  });
  expect(booked.status()).toBe(201);
  return (await booked.json()).id;
}

/** Each of the customer's future appointments by id: its status. */
async function statuses(context: BrowserContext): Promise<Record<string, string>> {
  const from = encodeURIComponent(new Date().toISOString());
  const list = await context.request.get(`${API_URL}/v1/appointments?limit=50&from=${from}`, {
    headers: await bearer(context),
  });
  const { items } = (await list.json()) as { items: { id: string; status: string }[] };
  return Object.fromEntries(items.map((item) => [item.id, item.status]));
}

function upcoming(page: Page) {
  return page.getByRole("heading", { name: "Upcoming" }).locator("xpath=following-sibling::*[1]");
}

async function pickDay(page: Page) {
  await page.getByRole("button", { name: dayButtonName(day), exact: true }).click();
  await expect(page.getByRole("button", { name: dayButtonName(day), exact: true })).toHaveAttribute("aria-pressed", "true");
}

async function pickTime(page: Page, time: string) {
  await page.getByRole("radiogroup", { name: "Available times" }).getByText(time, { exact: true }).click();
  await expect(page.getByRole("radio", { name: time })).toBeChecked();
}

const moveAlert = (page: Page) => page.getByRole("region", { name: "Confirm new time" }).getByRole("alert");

test("a signed-in customer sees upcoming appointments in the business's time", async ({ browser }) => {
  customer = await browser.newContext();
  await customer.addInitScript(countUntitled);
  const page = await customer.newPage();
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
  await expect(page.getByText("No upcoming appointments.")).toBeVisible();

  for (const [name, time] of [["nine", "09:00"], ["half", "09:30"], ["ten", "10:00"]]) {
    ids[name] = await seed(customer, accounts().booking, time);
  }
  await page.reload();
  // 09:00 in Kolkata is the evening before in Los Angeles: the label must say 9:00 AM
  for (const time of ["9:00 AM", "9:30 AM", "10:00 AM"]) {
    await expect(upcoming(page).locator("p", { hasText: label(time) })).toBeVisible();
  }
  await expect(page.getByText("Showing the last 30 days.")).toBeVisible();
  await expect(page.getByText("No appointments in the last 30 days.")).toBeVisible();
  await expectTitle(page, "My appointments · QueueLess");
  await noAxeViolations(page, "/me (upcoming)");
});

test("cancel asks first; Esc keeps it; a double click cancels once", async () => {
  const page = customer.pages()[0];
  const cancel = page.getByRole("button", { name: `Cancel ${label("10:00 AM")}` });
  await cancel.click();
  const dialog = page.getByRole("dialog", { name: "Cancel this appointment?" });
  await expect(dialog).toBeVisible();
  await noAxeViolations(page, "/me (cancel dialog)");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(cancel).toBeFocused();

  await page.route("**/v1/appointments/*", async (route) => {
    if (route.request().method() === "DELETE") await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  const deletes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "DELETE") deletes.push(request.url());
  });
  await cancel.click();
  await dialog.getByRole("button", { name: "Cancel appointment" }).dblclick();
  const done = page.getByRole("status").filter({ hasText: "Cancelled" });
  await expect(done).toHaveText(`Cancelled ${label("10:00 AM")}.`);
  await expect(done).toBeFocused();
  await page.unrouteAll({ behavior: "wait" });
  expect(deletes).toHaveLength(1);
  await expect(upcoming(page).getByText(label("10:00 AM"))).toHaveCount(0);
  expect((await statuses(customer))[ids.ten]).toBe("cancelled");
  await expectTitle(page, "My appointments · QueueLess");
  await noAxeViolations(page, "/me (cancelled)");
});

test("an appointment moves to a new time; the old one is marked rescheduled", async () => {
  const page = customer.pages()[0];
  await page.getByRole("link", { name: `Move ${label("9:30 AM")}` }).click();
  await expect(page.getByRole("heading", { name: `Move appointment at ${accounts().booking.name}` })).toBeVisible();
  await expect(page.getByText(`Now: ${label("9:30 AM")}`)).toBeVisible();
  await expectTitle(page, `Move appointment at ${accounts().booking.name} · QueueLess`);
  await pickDay(page); // a router.replace
  await expectTitle(page, `Move appointment at ${accounts().booking.name} · QueueLess`);
  await pickTime(page, "11:00 AM");
  await noAxeViolations(page, "/me/reschedule (confirm)");
  const answer = page.waitForResponse((response) => response.url().endsWith(`/v1/appointments/${ids.half}/reschedule`));
  await page.getByRole("button", { name: "Move appointment" }).click();
  await expect(page.getByRole("heading", { name: "Moved" })).toBeVisible();
  const moved = await (await answer).json();
  expect(moved.rescheduled_from_id).toBe(ids.half);
  ids.eleven = moved.id;
  await expectTitle(page, `Move appointment at ${accounts().booking.name} · QueueLess`);
  await noAxeViolations(page, "/me/reschedule (moved)");
  const now = await statuses(customer);
  expect([now[ids.half], now[ids.eleven]]).toEqual(["rescheduled", "booked"]);
});

test("a time taken while choosing leaves the appointment as it was and offers the next ones", async () => {
  const page = customer.pages()[0];
  await page.goto(`/me/reschedule/${ids.nine}`);
  await pickDay(page);
  await pickTime(page, "11:30 AM");
  ids.half11 = await seed(customer, accounts().booking, "11:30"); // the same time, booked first
  await page.getByRole("button", { name: "Move appointment" }).click();
  const alert = page.getByRole("alert").filter({ hasText: "That time was just taken" });
  await expect(alert).toBeFocused();
  await expect(alert).toContainText("Your current appointment hasn't changed.");
  await expect(alert.getByRole("button", { name: "12:00 PM" })).toBeVisible();
  await noAxeViolations(page, "/me/reschedule (SLOT_TAKEN)");
  expect((await statuses(customer))[ids.nine]).toBe("booked");
});

test("cancelled in one tab, the move in another tab is refused", async () => {
  const [first] = customer.pages();
  const second = await customer.newPage();
  await second.goto(`/me/reschedule/${ids.nine}`);
  await pickDay(second);
  await pickTime(second, "12:00 PM");
  await first.goto("/me");
  await first.getByRole("button", { name: `Cancel ${label("9:00 AM")}` }).click();
  await first.getByRole("button", { name: "Cancel appointment" }).click();
  await expect(first.getByRole("status").filter({ hasText: "Cancelled" })).toBeVisible();
  await second.getByRole("button", { name: "Move appointment" }).click();
  await expect(moveAlert(second)).toHaveText("This appointment can't be changed any more.");
  await second.close();
});

test("one neutral page for an id that cannot be moved, whoever's it is", async () => {
  const page = customer.pages()[0];
  [ids.other] = insertAppointments(ids.eleven, "booked", [new Date(Date.now() + (40 * 24 + index) * 60 * MINUTE)], true);
  const seen = new Set<string>();
  for (const id of [ids.ten, ids.half, ids.other, "not-an-appointment"]) {
    await page.goto(`/me/reschedule/${id}`);
    await expect(page.getByRole("heading", { name: "This appointment can't be moved" })).toBeVisible();
    await expectTitle(page, "Move appointment · QueueLess");
    seen.add(await page.locator("main").innerHTML());
  }
  expect(seen.size).toBe(1); // cancelled, rescheduled, someone else's and a wrong id look the same
  await noAxeViolations(page, "/me/reschedule (cannot be moved)");
});

test("too close to its start to move: the cutoff message", async () => {
  const page = customer.pages()[0];
  ids.strict = await seed(customer, accounts().cutoff, "09:00");
  await page.goto("/me");
  await page.getByRole("link", { name: `Move ${label("9:00 AM")}` }).click();
  await pickDay(page);
  await pickTime(page, "9:30 AM");
  await page.getByRole("button", { name: "Move appointment" }).click();
  await expect(moveAlert(page)).toHaveText("This appointment starts too soon to move. You can still cancel it.");
  await noAxeViolations(page, "/me/reschedule (cutoff)");
});

test("past appointments newest first and the live-ticket pointer", async () => {
  const page = customer.pages()[0];
  const now = Date.now();
  insertAppointments(ids.eleven, "completed", [new Date(now - 26 * 60 * MINUTE)]);
  insertAppointments(ids.eleven, "cancelled", [new Date(now - 2 * 60 * MINUTE)]);
  // checked in early for one starting soon; each project its own 30 minutes on the resource
  insertAppointments(ids.eleven, "checked_in", [new Date(now + (10 + 40 * index) * MINUTE)]);
  await page.goto("/me");
  const past = page.getByRole("heading", { name: "Past" }).locator("xpath=following-sibling::ul[1]/li");
  await expect(past).toHaveCount(2);
  await expect(past.nth(0)).toContainText("Cancelled");
  await expect(past.nth(1)).toContainText("Completed");
  await expect(page.getByText(/more than 50/)).toHaveCount(0);
  await noAxeViolations(page, "/me (past and pointer)");

  await page.getByRole("link", { name: "Checked in: see your live ticket" }).click();
  // the pointer opens the ticket list (this test's appointment was written to the database
  // without a ticket, so the list is empty)
  await expect(page.getByRole("heading", { name: "Your tickets" })).toBeVisible();
  await expect(page.getByText("You have no active tickets.")).toBeVisible();
  await expectTitle(page, "Your tickets · QueueLess");
  await noAxeViolations(page, "/ticket");
});

test("a browser clock two hours fast: lists usable, cancel and move still decided by the server", async ({ browser }) => {
  const state = await customer.storageState();
  await customer.close(); // the skewed context takes over the session (refresh tokens rotate)
  const skewed = await browser.newContext({ storageState: state });
  await skewed.addInitScript(countUntitled);
  await skewed.clock.install({ time: new Date(Date.now() + 120 * MINUTE) });
  // booked, starting within the next two hours: the skewed browser asks for "from now" too late
  insertAppointments(ids.strict, "booked", [new Date(Date.now() + (30 + 35 * index) * MINUTE)]);
  const page = await skewed.newPage();
  await page.goto("/me");
  const past = page.getByRole("heading", { name: "Past" }).locator("xpath=following-sibling::ul[1]");
  await expect(past.getByText(/Not checked in/)).toHaveCount(1); // listed under Past
  await expect(upcoming(page).getByRole("listitem")).toHaveCount(3); // 11:00, 11:30, cutoff 9:00
  await page.getByRole("button", { name: `Cancel ${label("11:30 AM")}` }).click();
  await page.getByRole("button", { name: "Cancel appointment" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Cancelled" })).toBeVisible();
  await page.getByRole("link", { name: `Move ${label("11:00 AM")}` }).click();
  await pickDay(page);
  await pickTime(page, "12:30 PM");
  await page.getByRole("button", { name: "Move appointment" }).click();
  await expect(page.getByRole("heading", { name: "Moved" })).toBeVisible();
  await expectTitle(page, `Move appointment at ${accounts().booking.name} · QueueLess`);
  const now = await statuses(skewed);
  expect([now[ids.half11], now[ids.eleven]]).toEqual(["cancelled", "rescheduled"]);

  // more than 50 in the last 30 days: said on the page, never a silent cut-off
  const hours = Array.from({ length: 51 }, (_, hour) => new Date(Date.now() - (3 + hour) * 60 * MINUTE));
  insertAppointments(ids.eleven, "completed", hours);
  await page.goto("/me");
  await expect(
    page.getByText("You have more than 50 appointments in the last 30 days. Only the earliest 50 are shown here."),
  ).toBeVisible();
  await noAxeViolations(page, "/me (over 50)");
  await skewed.close();
});
