import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { addDays, dayLabel, localToday, timeLabel } from "../lib/time";
import { ACCOUNTS_FILE } from "./global-setup";
import { API_URL, otpCodeFor, uniquePhone } from "./local-stack";

// The dialog focus race, forced. A modal's `close` event is its own task, so its handler can run
// after React has committed the state the confirmed action set - and after the app has focused
// the notice. Here `close()` is deferred 50 ms so that order happens every time, in every
// browser: the notice must still hold focus when the dialog is gone.
test.describe.configure({ mode: "serial" });

type Place = { id: string; name: string; service: string };
const accounts = (): { booking: Place } => JSON.parse(readFileSync(ACCOUNTS_FILE, "utf8"));

let customer: BrowserContext;
let page: Page;
let day: string;

const ZONE = "Asia/Kolkata";
const NINE = "09:00";

test.beforeAll(async ({ browser }, testInfo) => {
  // A day of this spec's own, clear of every other spec's fixtures.
  day = addDays(localToday(ZONE), 7 + ["chromium", "firefox", "webkit"].indexOf(testInfo.project.name));
  customer = await browser.newContext();
  await customer.addInitScript(() => {
    const listen = EventTarget.prototype.addEventListener;
    // A cheap focus trail, registered with the untouched addEventListener.
    const trail: [number, string, Element | null][] = [];
    (window as unknown as { trail: typeof trail }).trail = trail;
    for (const type of ["focusin", "focusout"]) {
      listen.call(document, type, ((event: Event) => trail.push([performance.now(), type, event.target as Element])) as EventListener, true);
    }
    // Hold the list refetch, so the opener is still on the page when the close handler runs:
    // in CI the restore landed 4 ms after the notice was focused and the refetch removed that
    // button 6 ms later, which is the only window in which the old code could steal focus.
    const fetched = window.fetch;
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      return url.includes("/v1/appointments?")
        ? new Promise((resolve) => setTimeout(() => resolve(fetched(input, init)), 300))
        : fetched(input, init);
    }) as typeof window.fetch;
    // The race, forced: a modal's `close` event is its own task, so its handler can run after
    // React has committed the confirmed action and focused the notice. Deferring the LISTENER
    // (not close() itself, which would keep the modal open and make the page inert) puts the
    // handler after the commit every time, in every browser.
    EventTarget.prototype.addEventListener = function (type, listener, options) {
      if (type !== "close" || typeof listener !== "function") {
        return listen.call(this, type, listener as EventListener, options as AddEventListenerOptions);
      }
      return listen.call(
        this,
        type,
        ((event: Event) => setTimeout(() => (listener as EventListener).call(this, event), 5)) as EventListener,
        options as AddEventListenerOptions,
      );
    };
  });
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

test("the notice keeps focus when the dialog's close event arrives late", async () => {
  const place = accounts().booking;
  const headers = { Authorization: `Bearer ${(await (await customer.request.post(`${API_URL}/v1/auth/refresh`)).json()).access_token}` };
  const free = await customer.request.get(
    `${API_URL}/v1/businesses/${place.id}/availability?service_id=${place.service}&date=${day}`,
    { headers },
  );
  const start = `${day}T${NINE}:00+05:30`;
  const slot = (await free.json()).slots.find((option: { start: string }) => option.start === start);
  expect(slot, `a free ${NINE} slot on ${day}`).toBeTruthy();
  const booked = await customer.request.post(`${API_URL}/v1/appointments`, {
    headers,
    data: { service_id: place.service, resource_id: slot.resource_id, start: slot.start },
  });
  expect(booked.ok(), await booked.text()).toBe(true);

  await page.reload();
  const label = `Checkup on ${dayLabel(day, "long")} at ${timeLabel(start, ZONE)}`;
  const cancel = page.getByRole("button", { name: `Cancel ${label}` });
  await expect(cancel).toBeVisible();
  await cancel.click();
  const dialog = page.getByRole("dialog", { name: "Cancel this appointment?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel appointment" }).click();

  const notice = page.getByRole("status").filter({ hasText: "Cancelled" });
  await expect(notice).toHaveText(`Cancelled ${label}.`);
  // Only once the deferred close has happened is the question settled: if the close handler
  // restores focus unconditionally, it has taken it off the notice by now.
  await expect(dialog).toBeHidden();
  try {
    await expect(notice).toBeFocused();
  } catch (failure) {
    const seen = await page.evaluate(() => {
      const describe = (node: Element | null) =>
        node ? `${node.tagName.toLowerCase()}${node.getAttribute("role") ? `[${node.getAttribute("role")}]` : ""}:${(node.textContent ?? "").slice(0, 28)}` : "null";
      const trail = (window as unknown as { trail: [number, string, Element | null][] }).trail;
      return {
        active: describe(document.activeElement),
        activeIsBody: document.activeElement === document.body,
        noticeStillThere: !!document.querySelector("[role=status]"),
        trail: trail.slice(-14).map(([at, type, node]) => `${at.toFixed(1)} ${type} ${describe(node)}`),
      };
    });
    console.log(`FOCUS TRAIL ${JSON.stringify(seen, null, 2)}`);
    throw failure;
  }
});
