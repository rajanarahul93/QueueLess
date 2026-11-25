// Runs before `npm run build` (the `prebuild` script). NEXT_PUBLIC_API_URL is inlined into the
// browser bundle at build time, and without it the bundle silently talks to localhost:8000, which
// is wrong for anything but a machine running the whole stack. `npm run dev` is not guarded.
// Opt out on purpose with ALLOW_LOCALHOST_API=true (CI's web job, which only builds to check).
const env = process.env;

if (env.NEXT_PUBLIC_API_URL?.trim() || env.ALLOW_LOCALHOST_API === "true") process.exit(0);

console.error(
  [
    "Build stopped: NEXT_PUBLIC_API_URL is not set.",
    "It is inlined into the browser bundle at build time; without it the app would call",
    "http://localhost:8000 from every visitor's browser.",
    "Set it to the API's public origin, e.g. NEXT_PUBLIC_API_URL=https://api.example.com.",
    "To build for localhost on purpose (local checks, CI), set ALLOW_LOCALHOST_API=true.",
  ].join("\n"),
);
process.exit(1);
