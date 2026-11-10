"use client";

import Link from "next/link";
import { Button } from "@/components/Button";
import { api, sendKeyed } from "@/lib/api/client";
import { ticketMessage } from "@/lib/api/errors";
import type { Queue, Token } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/AuthProvider";
import { ProblemAlert, useTakeTicket } from "@/lib/useTakeTicket";

/** Walk in: today's queue of this business. Signed out: sign in first and come back here. Staff,
 *  owners and admins see why they cannot join (the API takes customer accounts only). Nothing
 *  renders until the session is known, so there is no control before hydration. */
export function JoinQueue({ businessId }: { businessId: string }) {
  const { status, user } = useAuth();
  const { take, busy, wait, problem } = useTakeTicket(businessId, (failure) => ({ text: ticketMessage(failure) }));
  if (status === "loading" || status === "unreachable") return null; // nothing to offer until the session is known

  return (
    <section aria-labelledby="walk-in" className="mt-8">
      <h2 id="walk-in" className="text-h2 font-semibold">
        Walk in
      </h2>
      <p className="mt-1 text-body text-muted">Join today&apos;s queue without an appointment.</p>
      {status === "signed-out" ? (
        <Link
          href={`/login?next=${encodeURIComponent(`/b/${businessId}`)}`}
          className="mt-3 inline-flex min-h-12 items-center rounded-md border border-border-strong px-4 text-label font-medium"
        >
          Sign in to join the queue
        </Link>
      ) : user?.role !== "customer" ? (
        <p className="mt-3 text-body">Joining the queue is for customer accounts.</p>
      ) : (
        <>
          <Button
            type="button"
            className="mt-3 w-full"
            loading={busy}
            disabled={wait > 0}
            onClick={() =>
              void take(async (key) => {
                const queue = await api<Queue>(`/v1/businesses/${businessId}/queue`);
                return sendKeyed<Token>(`/v1/queues/${queue.id}/tokens`, "POST", {}, key);
              })
            }
          >
            {wait > 0 ? `Join the queue (wait ${wait} s)` : "Join the queue"}
          </Button>
          {problem && <ProblemAlert problem={problem} />}
        </>
      )}
    </section>
  );
}
