import { describe, expect, it } from "vitest";
import { safeNext } from "@/lib/auth/actions";

describe("safeNext (where sign-in returns to)", () => {
  it("keeps same-origin relative paths, query included", () => {
    expect(safeNext("/b/abc/book?service=s1&date=2026-11-03")).toBe("/b/abc/book?service=s1&date=2026-11-03");
    expect(safeNext("/me")).toBe("/me");
  });

  it.each([
    "https://evil.example/b",
    "http://evil.example",
    "//evil.example/path",
    "/\\evil.example",
    "javascript:alert(1)",
    "evil.example/b",
    "",
  ])("rejects %s", (value) => {
    expect(safeNext(value)).toBeNull();
  });

  it("rejects nothing given", () => {
    expect(safeNext(null)).toBeNull();
  });
});
