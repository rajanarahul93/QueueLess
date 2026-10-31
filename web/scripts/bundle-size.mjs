// Initial client JavaScript per route, gzipped, from Next's build manifests (no server): the
// root main files plus every entry file in the route's client manifest. The polyfill chunk is
// left out: it is loaded with noModule, which the supported browsers skip. Run after
// `next build`. `--write-baseline` rewrites bundle-baseline.json (commit it as a reviewed change).
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import { evaluate } from "./bundle-check.mjs";

const NEXT = ".next";
const BASELINE = "bundle-baseline.json";
const build = JSON.parse(readFileSync(join(NEXT, "build-manifest.json"), "utf8"));
const polyfills = new Set(build.polyfillFiles);

function* manifests(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* manifests(path);
    else if (entry.name === "page_client-reference-manifest.js") yield path;
  }
}

/** "/(auth)/login/page" -> "/login"; "/page" -> "/". Route groups do not appear in URLs. */
function routeOf(key) {
  const parts = key.split("/").filter((part) => part && part !== "page" && !/^\(.*\)$/.test(part));
  return `/${parts.join("/")}`;
}

const sizes = {};
for (const path of manifests(join(NEXT, "server", "app"))) {
  const source = readFileSync(path, "utf8");
  const key = /__RSC_MANIFEST\["([^"]+)"\]/.exec(source)[1];
  const route = routeOf(key);
  if (route.startsWith("/_")) continue; // Next's own error pages
  const entries = JSON.parse(/"entryJSFiles":(\{[^}]*\})/.exec(source)[1]);
  const files = new Set([...build.rootMainFiles, ...Object.values(entries).flat()]);
  let bytes = 0;
  for (const file of files) {
    if (!polyfills.has(file)) bytes += gzipSync(readFileSync(join(NEXT, file))).length;
  }
  sizes[route] = Math.round((bytes / 1024) * 10) / 10;
}

if (process.argv.includes("--write-baseline")) {
  writeFileSync(BASELINE, `${JSON.stringify(sizes, Object.keys(sizes).sort(), 2)}\n`);
  console.log(`wrote ${BASELINE}`);
}
const { lines, failures } = evaluate(sizes, JSON.parse(readFileSync(BASELINE, "utf8")));
console.log("Initial JS per route, gzipped (polyfills for old browsers excluded):");
lines.forEach((line) => console.log(`  ${line}`));
failures.forEach((failure) => console.error(`FAIL ${failure}`));
process.exit(failures.length ? 1 : 0);
