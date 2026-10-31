import { defineConfig, devices } from "@playwright/test";

// Runs against the real Compose stack (web :3000, api :8000). One worker: the projects share
// one API whose login endpoints are rate limited, and OTP codes are read from its log.
export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.WEB_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // Every browser runs in Los Angeles while the businesses are in Kolkata: any time shown in
    // the browser's zone instead of the business's would be wrong in every test.
    timezoneId: "America/Los_Angeles",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
