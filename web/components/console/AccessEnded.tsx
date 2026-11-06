"use client";

import Link from "next/link";
import { Button } from "@/components/Button";
import { logout } from "@/lib/auth/session";

/** Shown when this business is no longer ours: the stream said `revoked`, or a staff call came
 *  back 404. Every stream for the queue is closed by then, so nothing keeps retrying. */
export function AccessEnded() {
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold" tabIndex={-1} ref={(node) => node?.focus()}>
        Your access to this business has ended
      </h1>
      <p className="mt-2 text-body text-muted">
        An owner removed you from it, or it was closed. Nothing you did caused this.
      </p>
      <div className="mt-6 flex flex-wrap items-center gap-3">
        <Button onClick={() => void logout()}>Sign out</Button>
        <Link href="/console" className="min-h-12 px-2 text-label text-primary underline">
          Choose another business
        </Link>
      </div>
    </section>
  );
}
