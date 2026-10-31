import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OtpInput } from "@/components/OtpInput";
import { toE164 } from "@/lib/phone";

function Harness({ onComplete }: { onComplete: (code: string) => void }) {
  const [value, setValue] = useState("");
  return <OtpInput value={value} onChange={setValue} onComplete={onComplete} />;
}

const boxes = () => screen.getAllByRole("textbox") as HTMLInputElement[];

describe("OtpInput", () => {
  it("moves forward while typing and submits on the sixth digit", async () => {
    const done = vi.fn();
    render(<Harness onComplete={done} />);
    await userEvent.click(boxes()[0]);
    await userEvent.keyboard("12345");
    expect(boxes().map((box) => box.value).join("")).toBe("12345");
    expect(document.activeElement).toBe(boxes()[5]);
    expect(done).not.toHaveBeenCalled();
    await userEvent.keyboard("6");
    expect(done).toHaveBeenCalledWith("123456");
  });

  it("fills every box from a paste, ignoring non-digits", () => {
    const done = vi.fn();
    render(<Harness onComplete={done} />);
    fireEvent.paste(boxes()[0], { clipboardData: { getData: () => " 98-76 54 " } });
    expect(boxes().map((box) => box.value).join("")).toBe("987654");
    expect(done).toHaveBeenCalledWith("987654");
  });

  it("Backspace clears and moves back", async () => {
    render(<Harness onComplete={vi.fn()} />);
    await userEvent.click(boxes()[0]);
    await userEvent.keyboard("123{Backspace}{Backspace}");
    expect(boxes().map((box) => box.value).join("")).toBe("1");
  });

  it("has six labelled numeric boxes, the first one-time-code", () => {
    render(<Harness onComplete={vi.fn()} />);
    expect(boxes()).toHaveLength(6);
    expect(boxes()[0].getAttribute("autocomplete")).toBe("one-time-code");
    expect(boxes().every((box) => box.inputMode === "numeric")).toBe(true);
    expect(screen.getByLabelText("Digit 6")).toBeTruthy();
  });
});

describe("toE164", () => {
  it("formats what people type into strict E.164", () => {
    expect(toE164("91", "098765 43210")).toBe("+919876543210");
    expect(toE164("1", "(415) 555-0100")).toBe("+14155550100");
    expect(toE164("91", "123")).toBeNull(); // too short
    expect(toE164("44", "1".repeat(20))).toBeNull(); // too long
  });
});

describe("the login screen countdown", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("waits as long as Retry-After says, then lets the user try again", async () => {
    vi.doMock("next/navigation", () => ({
      useRouter: () => ({ replace: vi.fn() }),
      useSearchParams: () => new URLSearchParams(),
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ code: "RATE_LIMITED" }), {
          status: 429,
          headers: { "Retry-After": "24", "Content-Type": "application/json" },
        }),
      ),
    );
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const LoginPage = (await import("@/app/(auth)/login/page")).default;
    render(<LoginPage />);
    fireEvent.change(screen.getByLabelText("Mobile number"), { target: { value: "98765 43210" } });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Too many attempts. Try again in 24 s.");
    const waiting = await screen.findByRole("button", { name: "Send code (wait 24 s)" });
    expect(waiting).toHaveProperty("disabled", true);
    await act(async () => {
      vi.advanceTimersByTime(24_000);
    });
    expect(await screen.findByRole("button", { name: "Send code" })).toHaveProperty("disabled", false);
  });
});
