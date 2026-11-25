import { Inter } from "next/font/google";
import { connection } from "next/server";
import type { ReactNode } from "react";
import { DemoBanner } from "@/components/DemoBanner";
import { Header } from "@/components/Header";
import "./globals.css";
import { Providers } from "./providers";

// Variable font, Latin subset, self-hosted by next/font at build time.
const inter = Inter({ subsets: ["latin"], display: "swap", variable: "--font-inter" });

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Rendered per request so Next can put the proxy's CSP nonce on its scripts (report-only
  // for now; the cost of this is measured before the policy is enforced).
  await connection();
  return (
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <body className="min-h-screen font-sans antialiased">
        <Providers>
          <DemoBanner />
          <Header />
          <main>{children}</main>
        </Providers>
      </body>
    </html>
  );
}
