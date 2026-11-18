import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/owner/b1/settings",
  useSearchParams: () => new URLSearchParams(),
}));

const { OwnerSettings } = await import("@/components/owner/OwnerSettings");
const { asSettingsForm, changedSettings, settingsProblems } = await import("@/lib/owner/settings");
const { toApi } = await import("@/lib/owner/hours");

type Call = { url: string; method: string; body: Record<string, unknown> | null };
let calls: Call[];
let answer: (call: Call) => Response;
let business: Record<string, unknown>;

const WEEK = { mon: [{ open: "09:00", close: "17:00" }], tue: [{ open: "09:00", close: "17:00" }] };
const SAVED = {
  id: "b1",
  name: "North Clinic",
  category: "clinic",
  address: "12 Example Road",
  timezone: "Asia/Kolkata",
  open_hours: WEEK,
  settings: {
    grace_period_min: 5,
    reschedule_cutoff_min: 120,
    promotion_wait_limit_min: 20,
    running_late_threshold_min: 10,
    max_walk_ins: 0,
    walk_in_ratio: { walk_ins: 1, appointments: 2 },
  },
};

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const call: Call = { url, method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : null };
      calls.push(call);
      if (call.method === "GET") return Response.json(business);
      return answer(call);
    }),
  );
}

const show = (node: ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>);
const writes = () => calls.filter((call) => call.method === "PATCH");
const save = async () => {
  await act(async () => {
    fireEvent.submit(screen.getByRole("button", { name: "Save" }).closest("form")!);
  });
};

beforeEach(() => {
  calls = [];
  business = { ...SAVED };
  answer = (call) => Response.json({ ...SAVED, ...(call.body ?? {}) });
  serve();
});
afterEach(() => vi.unstubAllGlobals());

describe("the settings rules", () => {
  it("reads the server's values, falling back to the documented defaults", () => {
    const form = asSettingsForm({ ...SAVED, settings: {} } as never);
    expect(form).toMatchObject({ grace_period_min: "5", reschedule_cutoff_min: "120", promotion_wait_limit_min: "20", running_late_threshold_min: "10", walk_ins: "1", appointments: "2" });
  });

  it.each([
    [{ name: "" }, "name", "The business needs a name."],
    [{ name: "x".repeat(101) }, "name", "Keep the name to 100 characters."],
    [{ address: "x".repeat(301) }, "address", "Keep the address to 300 characters."],
    [{ grace_period_min: "61" }, "grace_period_min", "A whole number from 0 to 60."],
    [{ grace_period_min: "-1" }, "grace_period_min", "A whole number from 0 to 60."],
    [{ reschedule_cutoff_min: "10081" }, "reschedule_cutoff_min", "A whole number from 0 to 10080."],
    [{ promotion_wait_limit_min: "0" }, "promotion_wait_limit_min", "A whole number from 1 to 240."],
    [{ running_late_threshold_min: "121" }, "running_late_threshold_min", "A whole number from 1 to 120."],
    [{ max_walk_ins: "1001" }, "max_walk_ins", "A whole number from 0 to 1000."],
    [{ walk_ins: "0" }, "walk_ins", "A whole number from 1 to 10."],
    [{ appointments: "11" }, "appointments", "A whole number from 1 to 10."],
  ])("says what is wrong with %s", (over, field, copy) => {
    expect(settingsProblems({ ...asSettingsForm(SAVED as never), ...over })[field as "name"]).toBe(copy);
  });

  it("accepts the server's own bounds exactly", () => {
    const form = { ...asSettingsForm(SAVED as never), grace_period_min: "60", reschedule_cutoff_min: "10080", promotion_wait_limit_min: "240", running_late_threshold_min: "120", max_walk_ins: "1000", walk_ins: "10", appointments: "10" };
    expect(settingsProblems(form)).toEqual({});
  });

  it("sends only what changed, and the walk-in ratio as a whole", () => {
    const was = asSettingsForm(SAVED as never);
    expect(changedSettings(SAVED as never, was, WEEK, false, toApi)).toEqual({});
    expect(changedSettings(SAVED as never, { ...was, name: "South Clinic" }, WEEK, false, toApi)).toEqual({ name: "South Clinic" });
    expect(changedSettings(SAVED as never, { ...was, grace_period_min: "7" }, WEEK, false, toApi)).toEqual({ settings: { grace_period_min: 7 } });
    expect(changedSettings(SAVED as never, { ...was, walk_ins: "3" }, WEEK, false, toApi)).toEqual({ settings: { walk_in_ratio: { walk_ins: 3, appointments: 2 } } });
    // Either half changing sends BOTH halves as they are on screen: the API replaces the ratio
    // whole, so taking one side from the saved value would quietly undo the owner's edit.
    expect(changedSettings(SAVED as never, { ...was, appointments: "4" }, WEEK, false, toApi)).toEqual({ settings: { walk_in_ratio: { walk_ins: 1, appointments: 4 } } });
    expect(changedSettings(SAVED as never, { ...was, walk_ins: "3", appointments: "4" }, WEEK, false, toApi)).toEqual({ settings: { walk_in_ratio: { walk_ins: 3, appointments: 4 } } });
  });

  it("an emptied address is sent as null, which is how the API clears it", () => {
    const was = asSettingsForm(SAVED as never);
    expect(changedSettings(SAVED as never, { ...was, address: "" }, WEEK, false, toApi)).toEqual({ address: null });
  });

  it("the hours go only when they were edited", () => {
    const was = asSettingsForm(SAVED as never);
    expect(changedSettings(SAVED as never, was, WEEK, true, toApi)).toEqual({ open_hours: toApi(WEEK) });
  });
});

describe("the owner settings screen", () => {
  it("shows what the server has", async () => {
    show(<OwnerSettings businessId="b1" />);
    const value = (label: string) => (screen.getByLabelText(label) as HTMLInputElement).value;
    await screen.findByLabelText("Business name");
    expect(value("Business name")).toBe("North Clinic");
    expect(value("Address (optional)")).toBe("12 Example Road");
    expect(value("Grace period before a no-show (minutes)")).toBe("5");
    expect(screen.getByText(/It is .* there now/)).toBeTruthy();
    expect(screen.getByRole("group", { name: "Opening hours" })).toBeTruthy();
  });

  it("a read that fails offers Retry", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 })));
    show(<OwnerSettings businessId="b1" />);
    expect(await screen.findByText("We can't do that right now. Please try again in a moment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  it("saving sends only the field that changed, then reads the business again", async () => {
    show(<OwnerSettings businessId="b1" />);
    fireEvent.change(await screen.findByLabelText("Business name"), { target: { value: "South Clinic" } });
    await save();
    expect(writes()).toHaveLength(1);
    expect(writes()[0].body).toEqual({ name: "South Clinic" });
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved"));
    expect(calls.filter((call) => call.method === "GET").length).toBeGreaterThan(1);
  });

  it("saving nothing sends nothing", async () => {
    show(<OwnerSettings businessId="b1" />);
    await screen.findByLabelText("Business name");
    await save();
    expect(writes()).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toBe("Nothing to save");
  });

  it("a number out of the server's range is refused before sending, and takes focus", async () => {
    show(<OwnerSettings businessId="b1" />);
    fireEvent.change(await screen.findByLabelText("Grace period before a no-show (minutes)"), { target: { value: "61" } });
    await save();
    expect(writes()).toHaveLength(0);
    expect(screen.getByText("A whole number from 0 to 60.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Grace period before a no-show (minutes)"));
  });

  it("hours that cannot work are refused before sending", async () => {
    show(<OwnerSettings businessId="b1" />);
    await screen.findByLabelText("Business name");
    for (const day of ["Monday", "Tuesday"]) {
      await userEvent.click(screen.getByRole("button", { name: `Close ${day} all day` }));
    }
    await save();
    expect(writes()).toHaveLength(0);
    expect(screen.getByText("Open on at least one day, so people can book.")).toBeTruthy();
  });

  it("the picker shows the business's own zone even when the browser lists it under another name", async () => {
    show(<OwnerSettings businessId="b1" />);
    const picker = (await screen.findByLabelText("Time zone")) as HTMLSelectElement;
    expect(picker.value).toBe("Asia/Kolkata");
  });

  it("the server's refusal of a time-zone change is shown on the field", async () => {
    answer = () =>
      Response.json({ code: "VALIDATION_ERROR", errors: [{ loc: ["body", "timezone"], msg: "active appointments", type: "value_error" }] }, { status: 422 });
    show(<OwnerSettings businessId="b1" />);
    const picker = (await screen.findByLabelText("Time zone")) as HTMLSelectElement;
    const other = [...picker.options].map((option) => option.value).find((value) => value !== "Asia/Kolkata")!;
    fireEvent.change(picker, { target: { value: other } });
    await save();
    expect(await screen.findByText("You can't change the time zone while appointments are active.")).toBeTruthy();
  });

  it("a double submit sends one request", async () => {
    show(<OwnerSettings businessId="b1" />);
    fireEvent.change(await screen.findByLabelText("Business name"), { target: { value: "South Clinic" } });
    const form = screen.getByRole("button", { name: "Save" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(writes()).toHaveLength(1);
  });

  it.each([
    [429, { code: "RATE_LIMITED" }, "Too many attempts. Please wait and try again."],
    [403, { code: "FORBIDDEN" }, "You don't have access to this."],
  ])("says what a %s means", async (status, body, copy) => {
    answer = () => Response.json(body, { status });
    show(<OwnerSettings businessId="b1" />);
    fireEvent.change(await screen.findByLabelText("Business name"), { target: { value: "South Clinic" } });
    await save();
    expect(await screen.findByText(copy)).toBeTruthy();
  });
});
