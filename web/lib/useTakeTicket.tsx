"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { api, ApiError, newIdempotencyKey } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { ActiveToken, Token } from "@/lib/api/types";
import { Alert } from "@/components/Alert";
import { useCountdown } from "@/lib/useCountdown";

export type Problem = { text: string; link?: { href: string; label: string } };

/** Where "you already have a ticket in this queue" should lead: that ticket, found in the
 *  customer's active list, else the list of their tickets (never a dead end). */
export async function existingTicketHref(businessId: string): Promise<string> {
  try {
    const active = await api<ActiveToken[]>("/v1/tokens?active=true");
    const mine = active.find((ticket) => ticket.business_id === businessId);
    return mine ? `/ticket/${mine.id}` : "/ticket";
  } catch {
    return "/ticket";
  }
}

/** One "take a ticket" press (check in or join): one request at a time, one Idempotency-Key per
 *  action kept until it succeeds (a retry reuses it), the Retry-After countdown, and the ticket
 *  opened on success. `explain` turns any other refusal into words for this screen (null: the
 *  screen says it elsewhere). */
export function useTakeTicket(
  businessId: string,
  explain: (failure: unknown) => Problem | null = (failure) => ({ text: errorMessage(failure) }),
) {
  const router = useRouter();
  const inFlight = useRef(false);
  const action = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [wait, startWait] = useCountdown();

  async function take(send: (key: string) => Promise<Token>) {
    if (inFlight.current) return; // a second press while the first is out does nothing
    inFlight.current = true;
    setBusy(true);
    setProblem(null);
    action.current ??= newIdempotencyKey();
    try {
      const ticket = await send(action.current);
      action.current = null;
      router.push(`/ticket/${ticket.id}`);
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "ALREADY_IN_QUEUE") {
        const href = await existingTicketHref(businessId);
        setProblem({ text: errorMessage(failure), link: { href, label: href === "/ticket" ? "My tickets" : "Open your ticket" } });
      } else {
        setProblem(explain(failure));
        if (failure instanceof ApiError && failure.code === "RATE_LIMITED" && failure.retryAfterSec) startWait(failure.retryAfterSec);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return { take, busy, wait, problem };
}

/** A refusal in words, with the way on when there is one. */
export function ProblemAlert({ problem }: { problem: Problem }) {
  return (
    <Alert>
      {problem.text}
      {problem.link && (
        <>
          {" "}
          <Link href={problem.link.href} className="underline">
            {problem.link.label}
          </Link>
        </>
      )}
    </Alert>
  );
}
