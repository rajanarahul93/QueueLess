"use client";

import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert } from "@/components/Alert";
import { StatusPill } from "@/components/StatusPill";
import { Button } from "@/components/Button";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api } from "@/lib/api/client";
import { leaveMessage } from "@/lib/api/errors";
import { returnFocus } from "@/lib/focus";
import type { Eta } from "@/lib/api/types";
import { ENDED, ticketKey, useLiveTicket, type Ticket } from "@/lib/stream/useLiveTicket";
import { timeLabel } from "@/lib/time";

export const ANNOUNCE_EVERY_MS = 20_000;

/** "12–18 min", or "Calculating…" without an estimate: never a single number. */
export function etaLabel(eta: Eta | null): string {
  return eta ? `${eta.min_minutes}–${eta.max_minutes} min` : "Calculating…";
}

/** What the polite region says: "You're next" at once, any other position at most every 20 s,
 *  always the latest. The first value is not announced (the page itself shows it). */
export function useThrottledAnnouncement(message: string, urgent: boolean): string {
  const [said, setSaid] = useState("");
  const baseline = useRef<string | null>(null);
  const last = useRef(0);
  useEffect(() => {
    if (!message) return;
    if (baseline.current === null) {
      baseline.current = message;
      return;
    }
    if (message === (said || baseline.current)) return;
    const wait = urgent ? 0 : Math.max(0, last.current + ANNOUNCE_EVERY_MS - Date.now());
    const timer = setTimeout(() => {
      last.current = Date.now();
      setSaid(message);
    }, wait);
    return () => clearTimeout(timer);
  }, [message, urgent, said]);
  return said;
}

function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}

const STATUS: Record<string, { label: string; tone: string; icon: ReactNode }> = {
  waiting: { label: "Waiting", tone: "waiting", icon: <path d="M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" /> },
  called: { label: "Called", tone: "called", icon: <path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4l2-2Zm4 4h4" /> },
  in_service: { label: "In service", tone: "in-service", icon: <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 9a7 7 0 0 1 14 0" /> },
  completed: { label: "Completed", tone: "completed", icon: <path d="m8 12 3 3 5-6M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" /> },
  no_show: { label: "No-show", tone: "no-show", icon: <path d="m9 9 6 6m0-6-6 6M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" /> },
  cancelled: { label: "Cancelled", tone: "cancelled", icon: <path d="m5.6 5.6 12.8 12.8M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" /> },
};

function StatusBadge({ status }: { status: string }) {
  const look = STATUS[status] ?? STATUS.waiting;
  return (
    <span
      className="inline-flex h-7 items-center gap-1 rounded-full px-3 text-label font-medium"
      style={{ color: `var(--status-${look.tone}-fg)`, background: `var(--status-${look.tone}-bg)` }}
    >
      <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
        {look.icon}
      </svg>
      {look.label}
    </span>
  );
}

const ENDED_TEXT: Record<string, string> = {
  completed: "You've been served. Thanks for visiting.",
  no_show: "You were marked as not here when called.",
  cancelled: "This ticket is cancelled.",
};

export function TicketTitle({ id }: { id: string }) {
  const ticket = useQuery<Ticket>({ queryKey: ticketKey(id), queryFn: skipToken, staleTime: Infinity }).data;
  const name = useQuery({ ...businessQuery(ticket?.business_id ?? ""), enabled: Boolean(ticket?.business_id) }).data?.name;
  return <title>{ticket && name ? `Ticket #${ticket.token_no} at ${name} · QueueLess` : "Your ticket · QueueLess"}</title>;
}

export function LiveTicket({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const { ticket, status, retry } = useLiveTicket(id);
  const business = useQuery({ ...businessQuery(ticket?.business_id ?? ""), enabled: Boolean(ticket?.business_id) }).data;
  const now = useNow(30_000);
  const focusTarget = useRef<HTMLHeadingElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const leaveButton = useRef<HTMLButtonElement | null>(null);
  const inFlight = useRef(false);
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  const stage = ticket ? (ENDED.includes(ticket.status) ? "ended" : ticket.status) : null;
  useEffect(() => {
    if (stage === "called" || stage === "ended") focusTarget.current?.focus();
  }, [stage]);
  const waiting = ticket?.status === "waiting";
  const position = waiting && ticket.ahead_count !== null ? ticket.ahead_count + 1 : null;
  const message = waiting ? (ticket.next ? "You're next" : position ? `Your position: ${position}` : "") : "";
  const announced = useThrottledAnnouncement(message, message === "You're next");

  async function leave() {
    if (inFlight.current) return; // a second click does nothing
    inFlight.current = true;
    setLeaving(true);
    setLeaveError(null);
    try {
      await api(`/v1/tokens/${id}`, { method: "DELETE" }); // 204 also when already cancelled
      dialog.current?.close();
      queryClient.setQueryData<Ticket>(ticketKey(id), (prev) => (prev ? { ...prev, status: "cancelled", ahead_count: null, eta: null, next: false, late: null } : prev));
    } catch (failure) {
      setLeaveError(leaveMessage(failure));
    } finally {
      inFlight.current = false;
      setLeaving(false);
    }
  }

  if (status === "gone") {
    return (
      <>
        <h1 className="text-h1 font-semibold">This ticket isn&apos;t available</h1>
        <p className="mt-2 text-body text-muted">It may have ended, or it belongs to another account.</p>
        <Link href="/me" className="mt-6 inline-block min-h-11 py-3 text-primary underline">
          Back to my appointments
        </Link>
      </>
    );
  }
  if (!ticket) return <DelayedSkeleton rows={3} />;

  const live = status === "live";
  const age = Math.max(0, now - ticket.updatedAt);
  const updated = age < 60_000 ? "Updated just now" : `Updated ${Math.floor(age / 60_000)} min ago`;
  const late = waiting && ticket.late && !dismissed ? ticket.late : null;

  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-label text-muted">{business?.name ?? " "}</p>
        <StatusPill status={status} onRetry={retry} />
      </div>
      <div className="mt-3 flex items-center justify-between gap-3">
        <h1 className="text-token-number font-semibold tabular-nums">#{ticket.token_no}</h1>
        <StatusBadge status={ticket.status} />
      </div>
      {ticket.joined_at && business && (
        <p className="text-caption text-muted">
          Joined at {timeLabel(ticket.joined_at, business.timezone)} ({business.timezone})
        </p>
      )}
      {status === "capped" && (
        <p className="mt-4 rounded-md border border-border bg-surface p-4 text-body">
          Too many open screens. Close another QueueLess tab or window; this one updates every 10 seconds.
        </p>
      )}

      <div aria-live="polite" className="sr-only">{announced}</div>
      <div aria-live="assertive" className="sr-only">
        {ticket.status === "called" ? "It's your turn — please go to the counter." : ""}
      </div>

      {waiting && (
        <section aria-label="Your place" className="mt-6 rounded-lg border border-border bg-surface p-6 text-center">
          {ticket.next ? (
            <p className="text-h1 font-semibold">You&apos;re next</p>
          ) : (
            <>
              <p className="text-label text-muted">Your position</p>
              <p className="text-display font-semibold tabular-nums">{position ?? "—"}</p>
            </>
          )}
          <p className="mt-4 text-body-lg">Estimated wait</p>
          <p className="text-display font-semibold tabular-nums">{etaLabel(ticket.eta)}</p>
          <p className={`mt-2 text-caption ${live ? "text-muted" : "text-waiting"}`}>
            {live ? updated : `May be out of date · ${updated}`}
          </p>
        </section>
      )}
      {late && (
        <div role="status" className="mt-4 flex items-start gap-2 rounded-md bg-waiting-bg p-4 text-waiting">
          <svg aria-hidden="true" viewBox="0 0 24 24" className="mt-1 h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" />
          </svg>
          <p className="text-body">
            Running about {late.delay_min} min behind. Your new estimate is {etaLabel(late.eta ?? ticket.eta)}.
          </p>
          <button type="button" onClick={() => setDismissed(true)} className="ml-auto min-h-11 shrink-0 px-2 text-label underline">
            Dismiss
          </button>
        </div>
      )}
      {ticket.status === "called" && (
        <section className="mt-6 rounded-lg border-2 p-6 text-center" style={{ borderColor: "var(--status-called-fg)", background: "var(--status-called-bg)" }}>
          <h2 ref={focusTarget} tabIndex={-1} className="text-h1 font-semibold" style={{ color: "var(--status-called-fg)" }}>
            It&apos;s your turn
          </h2>
          <p className="mt-2 text-body-lg">Please go to the counter.</p>
        </section>
      )}
      {ticket.status === "in_service" && <p className="mt-6 text-body-lg">You&apos;re being served.</p>}
      {stage === "ended" && (
        <section className="mt-6">
          <h2 ref={focusTarget} tabIndex={-1} className="text-h2 font-semibold">
            {ENDED_TEXT[ticket.status]}
          </h2>
          {ticket.business_id && (
            <Link href={`/b/${ticket.business_id}`} className="mt-4 inline-flex min-h-12 items-center rounded-md border border-border-strong px-4 text-label font-medium">
              Back to business
            </Link>
          )}
        </section>
      )}

      {(ticket.status === "waiting" || ticket.status === "called") && (
        <Button
          type="button"
          variant="outline"
          className="mt-8 w-full"
          onClick={(event) => {
            leaveButton.current = event.currentTarget;
            setLeaveError(null);
            dialog.current?.showModal();
          }}
        >
          Leave the queue
        </Button>
      )}
      {/* Esc closes it natively (= stay); focus goes back to the button that opened it. */}
      <dialog
        ref={dialog}
        aria-labelledby="leave-title"
        onClose={() => returnFocus(leaveButton.current, dialog.current)}
        className="m-auto w-[calc(100%-2rem)] max-w-[420px] rounded-lg border border-border bg-surface p-6 text-text shadow-2 backdrop:bg-black/40"
      >
        <h2 id="leave-title" className="text-h2 font-semibold">
          Leave the queue?
        </h2>
        <p className="mt-2 text-body">You&apos;ll lose your place. To come back you join again at the end.</p>
        {leaveError && <Alert>{leaveError}</Alert>}
        <div className="mt-6 flex flex-col gap-3">
          <Button type="button" variant="outline" onClick={() => dialog.current?.close()}>
            Stay in the queue
          </Button>
          <Button type="button" onClick={() => void leave()} loading={leaving}>
            Yes, leave
          </Button>
        </div>
      </dialog>
    </>
  );
}
