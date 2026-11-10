import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/owner",
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

const { OwnerHub } = await import("@/components/owner/OwnerHub");
const { CreateBusiness } = await import("@/components/owner/CreateBusiness");
const { OpenHoursEditor } = await import("@/components/owner/OpenHoursEditor");
const { MAX_INTERVALS, MIDNIGHT, defaultHours, hoursProblem, toApi } = await import("@/lib/owner/hours");
const { myBusinessesQuery } = await import("@/components/console/BusinessPicker");

type Call = { url: string; method: string; key: string | null; body: Record<string, unknown> | null };
let calls: Call[];
let mine: { id: string; name: string; role: string; timezone: string }[];
let answer: (call: Call) => Response;
let hold: boolean;
let held: (() => void)[];

function serve() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const call: Call = {
        url,
        method: init.method ?? "GET",
        key: new Headers(init.headers).get("Idempotency-Key"),
        body: init.body ? JSON.parse(String(init.body)) : null,
      };
      calls.push(call);
      if (url.includes("/businesses/mine")) {
        return Response.json(
          mine.map((item) => ({ ...item, category: "clinic", address: null, open_hours: {} })),
        );
      }
      if (hold) await new Promise<void>((resolve) => held.push(resolve));
      return answer(call);
    }),
  );
}

let client: QueryClient;
function show(node: ReactNode) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const created = (over: Record<string, unknown> = {}) =>
  Response.json({ id: "b9", name: "North Clinic", category: "clinic", address: null, timezone: "Asia/Kolkata", open_hours: {}, ...over }, { status: 201 });

beforeEach(() => {
  calls = [];
  held = [];
  hold = false;
  mine = [];
  answer = () => created();
  replace.mockClear();
  window.history.replaceState({}, "", "/owner");
  serve();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- the week's rules, on their own ---

describe("opening hours", () => {
  it("starts as a plain working week, with the weekend closed", () => {
    const hours = defaultHours();
    expect(Object.keys(hours)).toEqual(["mon", "tue", "wed", "thu", "fri"]);
    expect(hours.mon).toEqual([{ open: "09:00", close: "17:00" }]);
    expect(hours.sat).toBeUndefined();
  });

  it.each([
    [{}, "Open on at least one day, so people can book."],
    [{ mon: [{ open: "17:00", close: "09:00" }] }, "Monday closes before it opens."],
    [{ mon: [{ open: "09:00", close: "09:00" }] }, "Monday closes before it opens."],
    [{ mon: [{ open: "", close: "17:00" }] }, "Monday needs an opening and a closing time."],
    [
      { mon: [{ open: "08:00", close: "09:00" }, { open: "10:00", close: "11:00" }, { open: "12:00", close: "13:00" }, { open: "14:00", close: "15:00" }, { open: "16:00", close: "17:00" }] },
      "Monday has more than 4 periods.",
    ],
  ])("says what is wrong with %s", (hours, copy) => {
    expect(hoursProblem(hours)).toBe(copy);
  });

  it("accepts a good week, and sends only the open days", () => {
    const hours = { mon: [{ open: "09:00", close: MIDNIGHT }], sat: [] };
    expect(hoursProblem(hours)).toBeNull();
    expect(toApi(hours)).toEqual({ mon: [{ open: "09:00", close: MIDNIGHT }] });
  });

  it("takes four periods a day and no more", () => {
    const four = [
      { open: "08:00", close: "09:00" },
      { open: "10:00", close: "11:00" },
      { open: "12:00", close: "13:00" },
      { open: "14:00", close: "15:00" },
    ];
    expect(MAX_INTERVALS).toBe(4); // the API's own cap, mirrored here
    expect(hoursProblem({ mon: four })).toBeNull();
    expect(hoursProblem({ mon: [...four, { open: "16:00", close: "17:00" }] })).toBe("Monday has more than 4 periods.");
  });
});

// --- the editor ---

describe("the hours editor", () => {
  it("shows every day, including the ones that start closed", () => {
    show(<OpenHoursEditor hours={defaultHours()} onChange={() => {}} />);
    for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]) {
      expect(screen.getByText(day)).toBeTruthy();
    }
    expect(screen.getAllByText("Closed")).toHaveLength(2); // Saturday and Sunday
    expect(screen.getByRole("button", { name: "Open Saturday" })).toBeTruthy();
  });

  it("says how to stay open until midnight", () => {
    show(<OpenHoursEditor hours={defaultHours()} onChange={() => {}} />);
    expect(screen.getByText(/close at 11:59 PM/)).toBeTruthy();
  });

  it("gives every time input its own accessible name", () => {
    show(<OpenHoursEditor hours={{ mon: [{ open: "09:00", close: "12:00" }, { open: "13:00", close: "17:00" }] }} onChange={() => {}} />);
    expect(screen.getByLabelText("Monday, period 1, opens")).toBeTruthy();
    expect(screen.getByLabelText("Monday, period 2, opens")).toBeTruthy();
    expect(screen.getByLabelText("Monday, period 2, closes")).toBeTruthy();
  });

  it("ties the problem to the fieldset", () => {
    show(<OpenHoursEditor hours={{}} onChange={() => {}} error="Open on at least one day, so people can book." />);
    const group = screen.getByRole("group", { name: "Opening hours" });
    expect(group.getAttribute("aria-describedby")).toContain("hours-error");
    expect(screen.getByText("Open on at least one day, so people can book.")).toBeTruthy();
  });

  /** The editor as the form drives it: its own state, so a removal really re-renders. */
  function Week({ start }: { start: ReturnType<typeof defaultHours> }) {
    const [hours, setHours] = useState(start);
    return <OpenHoursEditor hours={hours} onChange={setHours} />;
  }

  it("adds and removes a period with the keyboard, and never drops focus to the body", async () => {
    const user = userEvent.setup();
    show(<Week start={{ mon: [{ open: "09:00", close: "12:00" }] }} />);
    screen.getByRole("button", { name: "Add a period on Monday" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByLabelText("Monday, period 2, opens")).toBeTruthy();

    screen.getByRole("button", { name: "Remove Monday period 2" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.queryByLabelText("Monday, period 2, opens")).toBeNull();
    // The button that had focus is gone: focus must land on the day, not on nothing.
    await waitFor(() => {
      expect(document.activeElement).not.toBe(document.body);
      expect(document.activeElement).not.toBe(null);
    });
    expect(screen.getByLabelText("Monday, period 1, opens")).toBe(document.activeElement);
  });

});

// --- the hub ---

describe("the owner hub", () => {
  it("says when there is no business yet", async () => {
    show(<OwnerHub />);
    expect(await screen.findByText("You have no business yet.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create business" })).toBeTruthy();
  });

  it("lists the owner's own businesses, with their zone and the screens that exist", async () => {
    mine = [{ id: "b1", name: "North Clinic", role: "owner", timezone: "Asia/Kolkata" }];
    show(<OwnerHub />);
    const row = await screen.findByRole("listitem");
    expect(within(row).getByRole("heading", { name: "North Clinic" })).toBeTruthy();
    expect(within(row).getByText("Asia/Kolkata")).toBeTruthy();
    expect(within(row).getByRole("link", { name: "Queue console" }).getAttribute("href")).toBe("/console/b1");
    expect(within(row).getByRole("link", { name: "Appointments today" }).getAttribute("href")).toBe("/console/b1/appointments");
    // Nothing that ships later is linked yet.
    for (const later of ["Services", "Staff", "Settings", "Dashboard"]) {
      expect(within(row).queryByRole("link", { name: later })).toBeNull();
    }
  });

  it("leaves a business this owner only works at off the hub", async () => {
    mine = [
      { id: "b1", name: "North Clinic", role: "owner", timezone: "Asia/Kolkata" },
      { id: "b2", name: "Someone Else's", role: "staff", timezone: "Asia/Kolkata" },
    ];
    show(<OwnerHub />);
    await screen.findByText("North Clinic");
    expect(screen.queryByText("Someone Else's")).toBeNull();
  });

  it("reads the new business's name out of the link it came back on", async () => {
    window.history.replaceState({}, "", "/owner?created=North%20Clinic");
    mine = [{ id: "b1", name: "North Clinic", role: "owner", timezone: "Asia/Kolkata" }];
    show(<OwnerHub />);
    await screen.findByText("North Clinic");
    expect(screen.getByRole("status").textContent).toBe("Created North Clinic");
  });
});

// --- the create form ---

async function fill() {
  fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "North Clinic" } });
  fireEvent.change(screen.getByLabelText("Category"), { target: { value: "clinic" } });
}

describe("creating a business", () => {
  it("sends what the screen shows, with one keyed request", async () => {
    show(<CreateBusiness />);
    await fill();
    // A zone this runtime really offers: the list's names differ between ICU builds.
    const picker = screen.getByLabelText("Time zone") as HTMLSelectElement;
    const zone = [...picker.options].map((option) => option.value).find((value) => value.startsWith("Europe/"))!;
    fireEvent.change(picker, { target: { value: zone } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    const sent = calls.filter((call) => call.url.endsWith("/v1/businesses"));
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe("POST");
    expect(sent[0].key).toMatch(/[0-9a-f-]{36}/);
    expect(sent[0].body).toMatchObject({ name: "North Clinic", category: "clinic", timezone: zone });
    expect(sent[0].body!.address).toBeUndefined();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/owner?created=North%20Clinic"));
  });

  it("sends exactly the week on screen, not the one it started with", async () => {
    show(<CreateBusiness />);
    await fill();
    // Close Monday, open Saturday: both changes must reach the request.
    fireEvent.click(screen.getByRole("button", { name: "Close Monday all day" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Saturday" }));
    fireEvent.change(screen.getByLabelText("Saturday, period 1, closes"), { target: { value: "13:00" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    const body = calls.find((call) => call.url.endsWith("/v1/businesses"))!.body as { open_hours: Record<string, unknown> };
    expect(body.open_hours.mon).toBeUndefined();
    expect(body.open_hours.sat).toEqual([{ open: "09:00", close: "13:00" }]);
    expect(Object.keys(body.open_hours)).toEqual(["tue", "wed", "thu", "fri", "sat"]);
  });

  it("refuses a week that cannot work, and sends nothing", async () => {
    show(<CreateBusiness />);
    await fill();
    for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
      fireEvent.click(screen.getByRole("button", { name: `Close ${day} all day` }));
    }
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect(screen.getByText("Open on at least one day, so people can book.")).toBeTruthy();
    expect(calls.filter((call) => call.url.endsWith("/v1/businesses"))).toHaveLength(0);
  });

  it("offers the browser's own zone even when the runtime's list leaves it out", async () => {
    // The bug this pins: this runtime answers "Asia/Calcutta" while browsers report
    // "Asia/Kolkata" (and older builds the other way round). A preselected zone with no option
    // of its own would quietly select whichever zone happens to come first.
    const own = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const spy = vi.spyOn(Intl as unknown as { supportedValuesOf: (key: string) => string[] }, "supportedValuesOf");
    spy.mockReturnValue(["America/New_York"]);
    try {
      show(<CreateBusiness />);
      const picker = screen.getByLabelText("Time zone") as HTMLSelectElement;
      expect([...picker.options].map((option) => option.value)).toContain(own);
      expect(picker.value).toBe(own);
    } finally {
      spy.mockRestore();
    }
  });

  it("a double press creates one business", async () => {
    show(<CreateBusiness />);
    await fill();
    const form = screen.getByRole("button", { name: "Create business" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(calls.filter((call) => call.url.endsWith("/v1/businesses"))).toHaveLength(1);
  });

  it("a new deliberate press gets its own key, the same press keeps hers", async () => {
    answer = () => Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 });
    show(<CreateBusiness />);
    await fill();
    const form = screen.getByRole("button", { name: "Create business" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
    });
    // The same press again (nothing edited): the retry must carry the same key.
    await act(async () => {
      fireEvent.submit(form);
    });
    // A different name is a different press, and gets a key of its own.
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "South Clinic" } });
    await act(async () => {
      fireEvent.submit(form);
    });
    const keys = calls.filter((call) => call.url.endsWith("/v1/businesses")).map((call) => call.key);
    expect(keys).toHaveLength(3);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
  });

  it("after a creation the key is spent: an identical second press gets a new one", async () => {
    show(<CreateBusiness />);
    await fill();
    const form = screen.getByRole("button", { name: "Create business" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
    });
    // Nothing edited, but the first press SUCCEEDED, so this is a second deliberate press: a
    // kept key would replay the first answer and the owner's second business never exists.
    await act(async () => {
      fireEvent.submit(form);
    });
    const keys = calls.filter((call) => call.url.endsWith("/v1/businesses")).map((call) => call.key);
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("after 5 s it says so, and the retry of that press reuses its key", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    hold = true;
    show(<CreateBusiness />);
    await fill();
    const form = screen.getByRole("button", { name: "Create business" }).closest("form")!;
    await act(async () => {
      fireEvent.submit(form);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4900);
    });
    // Not a moment before five seconds: a press that answers quickly must stay quiet.
    expect(screen.queryByText(/Still working/)).toBeNull(); // a regex: an exact string must carry the full stop
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(screen.getByText("Still working… check your businesses.")).toBeTruthy();
    hold = false;
    await act(async () => {
      fireEvent.submit(form);
    });
    const keys = calls.filter((call) => call.url.endsWith("/v1/businesses")).map((call) => call.key);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]); // the same deliberate press, retried
    held.forEach((release) => release());
    vi.useRealTimers();
  }, 20_000);

  it("reads the list again after creating, rather than trusting the answer", async () => {
    show(<CreateBusiness />);
    const reread = vi.spyOn(client, "invalidateQueries");
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    await waitFor(() => expect(replace).toHaveBeenCalled());
    // The hub shows what the server has, not what the create answered with.
    expect(reread).toHaveBeenCalledWith({ queryKey: myBusinessesQuery.queryKey });
  });

  it.each([
    [["body", "name"], "Give the business a name."],
    [["body", "category"], "Say what kind of business it is."],
  ])("puts the server's complaint on the right field", async (loc, copy) => {
    answer = () => Response.json({ code: "VALIDATION_ERROR", errors: [{ loc, msg: "bad", type: "value_error" }] }, { status: 422 });
    show(<CreateBusiness />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect(await screen.findByText(copy)).toBeTruthy();
  });

  it("shows the server's field errors on the fields themselves", async () => {
    answer = () =>
      Response.json(
        { code: "VALIDATION_ERROR", errors: [{ loc: ["body", "timezone"], msg: "unknown IANA timezone", type: "value_error" }] },
        { status: 422 },
      );
    show(<CreateBusiness />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect(await screen.findByText("Pick the time zone the business keeps.")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it.each([
    [Response.json({ code: "FORBIDDEN" }, { status: 403 }), "You don't have access to this."],
    [Response.json({ code: "SERVICE_UNAVAILABLE" }, { status: 503 }), "We can't do that right now."],
  ])("says what went wrong", async (response, copy) => {
    answer = () => response;
    show(<CreateBusiness />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect((await screen.findByRole("alert")).textContent).toContain(copy);
  });

  it("counts the rate limit down in the owner's words", async () => {
    answer = () => Response.json({ code: "RATE_LIMITED" }, { status: 429, headers: { "Retry-After": "120" } });
    show(<CreateBusiness />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect((await screen.findByRole("alert")).textContent).toContain("2 minutes");
  });

  it("a network failure says so and keeps what was typed", async () => {
    answer = () => {
      throw new TypeError("offline");
    };
    show(<CreateBusiness />);
    await fill();
    await act(async () => {
      fireEvent.submit(screen.getByRole("button", { name: "Create business" }).closest("form")!);
    });
    expect((await screen.findByRole("alert")).textContent).toContain("offline");
    expect((screen.getByLabelText("Business name") as HTMLInputElement).value).toBe("North Clinic");
  });
});
