import type { NextConfig } from "next";

// Static security headers for every response. The per-request policy (nonce, report-only) and the
// referrer policy are set by proxy.ts. frame-ancestors is enforced on its own here because the
// browser ignores it inside a report-only policy.
const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        ],
      },
    ];
  },
};

export default nextConfig;
