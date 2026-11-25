import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function apiUrl() {
  vi.resetModules();
  return (await import("@/lib/api/config")).API_URL;
}

describe("API_URL", () => {
  it("is the build value when set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test");
    expect(await apiUrl()).toBe("https://api.example.test");
  });

  it("falls back to localhost when unset or empty (only reachable with the build opt-out)", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", undefined);
    expect(await apiUrl()).toBe("http://localhost:8000");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    expect(await apiUrl()).toBe("http://localhost:8000");
  });
});
