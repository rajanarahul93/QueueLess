import type { MetadataRoute } from "next";

// Colours are the design tokens in app/globals.css (--primary, --background, light theme).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "QueueLess",
    short_name: "QueueLess",
    description: "Appointments and walk-in tokens in one live queue.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    theme_color: "#1d4ed8",
    background_color: "#f8fafc",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
