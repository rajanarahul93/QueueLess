// Initial client JavaScript of the customer routes, gzipped: every script the server-rendered
// HTML loads or preloads. Fails over the 200 KB budget. Run after `next build`; it starts
// `next start` on a spare port. Chunks fetched later on demand are not counted.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

const BUDGET_KB = 200;
const ROUTES = ["/", "/login"]; // the customer routes that exist so far
const PORT = 3123;
const server = spawn("npx", ["next", "start", "-p", String(PORT)], { stdio: "ignore" });

async function html(path) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`http://localhost:${PORT}${path}`);
      if (response.ok) return response.text();
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`${path} did not load`);
}

let failed = false;
try {
  for (const route of ROUTES) {
    const page = await html(route);
    const urls = new Set(
      [...page.matchAll(/(?:src|href)="(\/_next\/static\/[^"]+\.js)"/g)].map((match) => match[1]),
    );
    let total = 0;
    for (const url of [...urls].sort()) {
      const bytes = gzipSync(readFileSync(`.next${url.replace("/_next", "")}`)).length;
      total += bytes;
      console.log(`  ${(bytes / 1024).toFixed(1).padStart(6)} KB  ${url}`);
    }
    const kb = total / 1024;
    console.log(`${route}: ${urls.size} scripts, ${kb.toFixed(1)} KB gzipped (budget ${BUDGET_KB} KB)`);
    if (kb > BUDGET_KB) failed = true;
  }
} finally {
  server.kill();
}
process.exit(failed ? 1 : 0);
