import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api/client";
import { BOOKING_CODES, errorMessage, hasCopy, invalidFields } from "@/lib/api/errors";

const problem = (code: string, body: Record<string, unknown> = {}, retry: number | null = null) =>
  new ApiError(400, code, retry, { code, detail: "raw server detail", ...body });

describe("errorMessage", () => {
  it("maps each OTP code to fixed copy and never shows the server detail", () => {
    for (const code of ["OTP_INVALID", "OTP_EXPIRED", "RATE_LIMITED", "SERVICE_UNAVAILABLE"]) {
      const text = errorMessage(problem(code));
      expect(text).not.toContain("raw server detail");
      expect(text.length).toBeGreaterThan(10);
    }
    expect(errorMessage(problem("OTP_INVALID"))).toBe("That code isn't right. Check it and try again.");
  });

  it("puts Retry-After into the rate-limit message", () => {
    expect(errorMessage(problem("RATE_LIMITED", {}, 24))).toBe("Too many attempts. Try again in 24 s.");
    expect(errorMessage(problem("RATE_LIMITED", {}, 600))).toBe("Too many attempts. Try again in 10 minutes.");
  });

  it("falls back to generic copy for unknown codes and non-API errors", () => {
    expect(errorMessage(problem("SOMETHING_NEW"))).toBe("Something went wrong on our side. Please try again.");
    expect(errorMessage(new Error("boom"))).toBe("Something went wrong on our side. Please try again.");
  });
});

describe("invalidFields", () => {
  it("lists the last part of each rejected location", () => {
    const error = problem("VALIDATION_ERROR", { errors: [{ loc: ["body", "email"], msg: "x" }] });
    expect(invalidFields(error)).toEqual(["email"]);
    expect(invalidFields(problem("OTP_INVALID"))).toEqual([]);
  });
});

describe("the booking error copy", () => {
  it("has its own message for every booking code", () => {
    const missing = BOOKING_CODES.filter((code) => !hasCopy(code));
    expect(missing).toEqual([]);
    const messages = BOOKING_CODES.map((code) => errorMessage(problem(code)));
    expect(new Set(messages).size).toBe(BOOKING_CODES.length); // all distinct
  });

  it("counts down for RATE_LIMITED and SERVICE_UNAVAILABLE with Retry-After", () => {
    expect(errorMessage(problem("SERVICE_UNAVAILABLE", {}, 5))).toBe("We can't do that right now. Try again in 5 s.");
  });
});
