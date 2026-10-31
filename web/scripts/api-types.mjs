// Regenerates lib/api/schema.d.ts from the backend's OpenAPI schema. CI runs it and fails when
// the committed file differs. openapi-typescript runs through npx (pinned) because it still
// declares TypeScript 5 as a peer; npx gives it its own copy instead of ours.
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Only so the app factory can build its settings: nothing connects, values are thrown away.
const env = {
  ...process.env,
  APP_ENV: "local",
  DATABASE_URL: "postgresql+asyncpg://unused:unused@localhost/unused",
  REDIS_URL: "redis://localhost:6379/0",
  JWT_SECRET: randomBytes(32).toString("hex"),
  STREAM_TOKEN_SECRET: randomBytes(32).toString("hex"),
};
const dump =
  "import json; from app.main import create_app; " +
  "print(json.dumps(create_app().openapi(), indent=2, sort_keys=True))";
const schema = execFileSync("uv", ["run", "--frozen", "python", "-c", dump], {
  cwd: new URL("../../backend", import.meta.url),
  env,
  encoding: "utf8",
});
const file = join(mkdtempSync(join(tmpdir(), "openapi-")), "openapi.json");
writeFileSync(file, schema);
execFileSync(
  "npx",
  ["--yes", "openapi-typescript@7.13.0", file, "--output", "lib/api/schema.d.ts"],
  { stdio: "inherit" },
);
