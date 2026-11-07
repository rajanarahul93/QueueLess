"use client";

import Link from "next/link";
import { Button } from "@/components/Button";
import { logout } from "@/lib/auth/session";

/** Two different facts, two different sentences. `ended`: this business WAS ours and is not any
 *  more (the stream said `revoked`, or a staff call came back 404) — every stream for the queue
 *  is closed by then, so nothing keeps retrying. `missing`: the member list simply does not have
 *  it, which is what a mistyped or foreign URL looks like; nothing has been taken away. */
export function AccessEnded({ reason = "ended" }: { reason?: "ended" | "missing" }) {
  const ended = reason === "ended";
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <h1 className="text-h1 font-semibold" tabIndex={-1} ref={(node) => node?.focus()}>
        {ended ? "Your access to this business has ended" : "You don't have access to this business."}
      </h1>
      <p className="mt-2 text-body text-muted">
        {ended
          ? "An owner removed you from it, or it was closed. Nothing you did caused this."
          : "It isn't one of the businesses you work at. Check the link, or pick one of yours."}
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
