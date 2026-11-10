"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { myBusinessesQuery } from "@/components/console/BusinessPicker";
import { errorMessage } from "@/lib/api/errors";
import type { MyBusiness } from "@/lib/api/types";

/** The owner's own businesses. A staff membership is somebody else's business to run, not one
 *  to own, so only the owner rows are listed here; the console picker still shows both. */
export function OwnerHub() {
  const mine = useQuery({ ...myBusinessesQuery, retry: false });
  // The create form comes back with ?created=<name>; the line is read once, by the router's
  // own reader (the browser's URL is updated after the new page renders).
  const created = useSearchParams().get("created");
  const owned = (mine.data ?? []).filter((item: MyBusiness) => item.role === "owner");

  if (mine.isError) return <Alert>{errorMessage(mine.error)}</Alert>;

  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <h1 className="text-h1 font-semibold">Your business</h1>
      <p role="status" className="mt-2 text-label text-muted">
        {created ? `Created ${created}` : ""}
      </p>
      {!mine.data ? (
        <DelayedSkeleton rows={3} />
      ) : owned.length === 0 ? (
        <>
          <p className="mt-4 text-body text-muted">You have no business yet.</p>
          <Link href="/owner/new" className="mt-4 inline-block">
            <Button>Create business</Button>
          </Link>
        </>
      ) : (
        <>
          <ul className="mt-4 space-y-3">
            {owned.map((business) => (
              <li key={business.id} className="rounded-md border border-border bg-surface p-4">
                <h2 className="text-body-lg font-semibold">{business.name}</h2>
                <p className="mt-1 text-label text-muted">{business.timezone}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Link
                    href={`/console/${business.id}`}
                    className="inline-flex min-h-12 items-center rounded-md border border-border-strong bg-surface px-4 text-label font-medium"
                  >
                    Queue console
                  </Link>
                  <Link
                    href={`/console/${business.id}/appointments`}
                    className="inline-flex min-h-12 items-center rounded-md border border-border-strong bg-surface px-4 text-label font-medium"
                  >
                    Appointments today
                  </Link>
                </div>
              </li>
            ))}
          </ul>
          <Link href="/owner/new" className="mt-4 inline-block">
            <Button variant="outline">Create business</Button>
          </Link>
        </>
      )}
    </section>
  );
}
