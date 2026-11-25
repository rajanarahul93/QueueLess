import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DemoBanner } from "@/components/DemoBanner";
import LoginPage from "@/app/(auth)/login/page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const json = (body: unknown, status = 202) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function stubOtpRequest(body: unknown) {
  const fetchMock = vi.fn(async () => json(body));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function sendCode(user: ReturnType<typeof userEvent.setup>) {
  render(<LoginPage />);
  await user.type(screen.getByLabelText("Mobile number"), "9876543210");
  await user.click(screen.getByRole("button", { name: "Send code" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("demo banner", () => {
  it("shows only when the build flag is exactly true", () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    const { unmount } = render(<DemoBanner />);
    expect(screen.getByText("Demo environment: data may be reset")).toBeTruthy();
    unmount();

    for (const value of ["", "false", "1", "TRUE"]) {
      vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", value);
      const { container, unmount: again } = render(<DemoBanner />);
      expect(container.textContent).toBe("");
      again();
    }
  });

  it("is absent when the flag is not set at all", () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", undefined);
    const { container } = render(<DemoBanner />);
    expect(container.textContent).toBe("");
  });
});

describe("demo code on the sign-in screen", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("shows the code in a labelled status box that keeps the focus on the digits", async () => {
    stubOtpRequest({ demo_code: "123456" });
    const user = userEvent.setup();
    await sendCode(user);
    const box = await screen.findByRole("status");
    expect(box.textContent).toContain("Demo mode: no SMS is sent. Your code is 123456");
    expect(box.contains(document.activeElement)).toBe(false);
    expect(screen.getByLabelText("Digit 1")).toBeTruthy();
  });

  it("can be copied", async () => {
    stubOtpRequest({ demo_code: "123456" });
    const user = userEvent.setup();
    await sendCode(user);
    await user.click(await screen.findByRole("button", { name: "Copy code" }));
    expect(await navigator.clipboard.readText()).toBe("123456");
  });

  it("is not shown when the response has no demo_code", async () => {
    stubOtpRequest({});
    const user = userEvent.setup();
    await sendCode(user);
    await screen.findByLabelText("Digit 1");
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.body.textContent).not.toContain("Demo mode");
    expect(screen.queryByRole("button", { name: "Copy code" })).toBeNull();
  });

  it("ignores a demo_code that is not six digits", async () => {
    stubOtpRequest({ demo_code: "<img src=x onerror=alert(1)>" });
    const user = userEvent.setup();
    await sendCode(user);
    await screen.findByLabelText("Digit 1");
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.body.innerHTML).not.toContain("onerror");
  });

  it("goes away with the number it belongs to", async () => {
    stubOtpRequest({ demo_code: "123456" });
    const user = userEvent.setup();
    await sendCode(user);
    await screen.findByRole("status");
    await user.click(screen.getByRole("button", { name: "Use another number" }));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("does not carry an old code over to the next request", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const answers = [{ demo_code: "123456" }, {}];
    vi.stubGlobal("fetch", vi.fn(async () => json(answers.shift())));
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await sendCode(user);
    await screen.findByRole("status");
    await user.click(screen.getByRole("button", { name: "Use another number" }));
    await act(async () => void vi.advanceTimersByTime(31_000)); // the resend wait is over
    await user.click(screen.getByRole("button", { name: "Send code" }));
    await screen.findByLabelText("Digit 1");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is kept in memory only: no storage, no URL, no console", async () => {
    stubOtpRequest({ demo_code: "123456" });
    const logged: unknown[][] = [];
    for (const method of ["log", "info", "warn", "error", "debug"] as const) {
      vi.spyOn(console, method).mockImplementation((...args) => void logged.push(args));
    }
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    await sendCode(user);
    await screen.findByRole("status");
    await waitFor(() => expect(screen.getByLabelText("Digit 1")).toBeTruthy());
    expect(setItem).not.toHaveBeenCalled();
    expect(JSON.stringify({ ...localStorage })).not.toContain("123456");
    expect(JSON.stringify({ ...sessionStorage })).not.toContain("123456");
    expect(window.location.href).not.toContain("123456");
    expect(JSON.stringify(logged)).not.toContain("123456");
    vi.restoreAllMocks();
  });
});
