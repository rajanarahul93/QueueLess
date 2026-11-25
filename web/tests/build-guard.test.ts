import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SCRIPT = resolve(process.cwd(), "scripts/check-build-env.mjs"); // vitest runs from web/

// A clean environment: only what the script needs to start, so a value set on the machine
// running the tests can never leak in.
function guard(env: Record<string, string>) {
  const run = spawnSync(process.execPath, [SCRIPT], {
    env: { PATH: process.env.PATH ?? "", NODE_ENV: "production", ...env },
    encoding: "utf8",
  });
  return { code: run.status, out: `${run.stdout}${run.stderr}` };
}

describe("build guard", () => {
  it("stops the build when NEXT_PUBLIC_API_URL is not set, and says how to go on", () => {
    const { code, out } = guard({});
    expect(code).toBe(1);
    expect(out).toContain("NEXT_PUBLIC_API_URL is not set");
    expect(out).toContain("ALLOW_LOCALHOST_API=true");
  });

  it("treats an empty value as not set (a Compose build arg with no value is empty)", () => {
    expect(guard({ NEXT_PUBLIC_API_URL: "" }).code).toBe(1);
    expect(guard({ NEXT_PUBLIC_API_URL: "   " }).code).toBe(1);
  });

  it("passes when the URL is set", () => {
    expect(guard({ NEXT_PUBLIC_API_URL: "https://api.example.test" }).code).toBe(0);
  });

  it("passes with the explicit opt-out, and only for the exact word true", () => {
    expect(guard({ ALLOW_LOCALHOST_API: "true" }).code).toBe(0);
    for (const value of ["", "1", "yes", "TRUE", "false"]) {
      expect(guard({ ALLOW_LOCALHOST_API: value }).code).toBe(1);
    }
  });

  it("is what `npm run build` runs first", async () => {
    const { default: pkg } = await import("../package.json");
    expect(pkg.scripts.prebuild).toBe("node scripts/check-build-env.mjs");
    expect(pkg.scripts.dev).toBe("next dev"); // local development is not guarded
  });

  it("is inside the Docker build context (the image runs `npm run build`)", () => {
    const rules = readFileSync(resolve(process.cwd(), ".dockerignore"), "utf8").split("\n");
    expect(rules).toContain("!scripts/check-build-env.mjs");
    expect(rules).not.toContain("scripts"); // a bare `scripts` would drop the guard again
  });
});
