"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { businessQuery } from "@/components/booking/BusinessDetail";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { api } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { Appointment, AppointmentPage, Business } from "@/lib/api/types";
import { dayLabel, localToday, timeLabel } from "@/lib/time";

export const PAST_DAYS = 30;
const DAY_MS = 86_400_000;

type Kind = "upcoming" | "past" | "checked_in" | "in_service";

// The API filters one status at a time and sorts by start ascending only. Upcoming = booked
// from now on: at most 5 (the booking cap), so one page. Past = the last 30 days, the earliest
// 50 of them. Checked in / in service: at most one at a time (one active ticket per customer).
// "now" is the browser's clock; the server still decides every cancel and move.
function search(kind: Kind, now: number): string {
  const iso = (ms: number) => new Date(ms).toISOString();
  const since = iso(now - PAST_DAYS * DAY_MS);
  const filter: Record<string, string> =
    kind === "upcoming"
      ? { status: "booked", from: iso(now) }
      : kind === "past"
        ? { from: since, to: iso(now) }
        : { status: kind, from: since };
  return new URLSearchParams({ ...filter, limit: "50" }).toString();
}

export function appointmentsQuery(kind: Kind, enabled = true) {
  return {
    queryKey: ["appointments", kind],
    queryFn: () => api<AppointmentPage>(`/v1/appointments?${search(kind, Date.now())}`),
    staleTime: 30_000,
    retry: false,
    enabled,
  };
}

/** "Checkup on Mon, Nov 2 at 9:00 AM", in the business's zone. */
export function describe(item: Appointment, business: Business): string {
  const day = localToday(business.timezone, new Date(item.start));
  return `${item.service_name} on ${dayLabel(day, "long")} at ${timeLabel(item.start, business.timezone)}`;
}

const PAST_STATUS: Record<string, string> = {
  booked: "Not checked in",
  checked_in: "Checked in",
  in_service: "In service",
  completed: "Completed",
  no_show: "Missed",
  cancelled: "Cancelled",
  rescheduled: "Moved",
};

const KINDS: Kind[] = ["upcoming", "past", "checked_in", "in_service"];
const action = "flex min-h-12 items-center justify-center rounded-md border border-border-strong px-4 text-label font-medium";

export function MyAppointments() {
  const queryClient = useQueryClient();
  const lists = useQueries({ queries: KINDS.map((kind) => appointmentsQuery(kind)) });
  const [upcoming, past, checkedIn, inService] = lists;
  const ids = [...new Set(lists.flatMap((list) => list.data?.items ?? []).map((item) => item.business_id))];
  const businesses = useQueries({ queries: ids.map(businessQuery) });
  const byId = new Map(businesses.flatMap((business) => (business.data ? [[business.data.id, business.data]] : [])));

  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const inFlight = useRef(false);
  const doneLine = useRef<HTMLParagraphElement>(null);
  const [target, setTarget] = useState<{ item: Appointment; label: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    if (done) doneLine.current?.focus();
  }, [done]);

  function ask(item: Appointment, label: string, button: HTMLButtonElement) {
    trigger.current = button;
    setTarget({ item, label });
    setError(null);
    dialog.current?.showModal();
  }

  async function cancel() {
    if (inFlight.current || !target) return; // a second click does nothing
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api(`/v1/appointments/${target.item.id}`, { method: "DELETE" }); // 204 also if already cancelled
      dialog.current?.close();
      setTarget(null);
      setDone(`Cancelled ${target.label}.`);
      void queryClient.invalidateQueries({ queryKey: ["availability", target.item.business_id] });
    } catch (failure) {
      setError(errorMessage(failure)); // e.g. it started or was moved meanwhile
    } finally {
      void queryClient.invalidateQueries({ queryKey: ["appointments"] });
      inFlight.current = false;
      setBusy(false);
    }
  }

  const failed = [...lists, ...businesses].find((query) => query.isError);
  if (failed) return <Alert>{errorMessage(failed.error)}</Alert>;
  if ([...lists, ...businesses].some((query) => query.isPending)) return <DelayedSkeleton rows={3} />;

  const at = (item: Appointment) => byId.get(item.business_id) as Business;
  const live = [...(checkedIn.data?.items ?? []), ...(inService.data?.items ?? [])];
  const earlier = [...(past.data?.items ?? [])].reverse(); // newest first

  return (
    <>
      {done && (
        <p ref={doneLine} tabIndex={-1} role="status" className="mt-4 rounded-md border border-border bg-surface p-4 text-body">
          {done}
        </p>
      )}
      {live.map((item) => (
        <p key={item.id} className="mt-4 text-body">
          <Link href="/ticket" className="text-primary underline">
            {item.status === "in_service" ? "In service" : "Checked in"}: see your live ticket
          </Link>
        </p>
      ))}

      <h2 className="mt-8 text-h2 font-semibold">Upcoming</h2>
      {upcoming.data?.items.length ? (
        <ul className="mt-3 space-y-3">
          {upcoming.data.items.map((item) => {
            const business = at(item);
            const label = describe(item, business);
            return (
              <li key={item.id} className="rounded-md border border-border bg-surface p-4">
                <p className="text-body-lg font-semibold">{label}</p>
                <p className="text-label text-muted">
                  {business.name} · times in {business.timezone}
                </p>
                <div className="mt-3 flex gap-3">
                  <Link href={`/me/reschedule/${item.id}`} className={action}>
                    Move{" "}
                    <span className="sr-only">{label}</span>
                  </Link>
                  <Button type="button" variant="outline" onClick={(event) => ask(item, label, event.currentTarget)}>
                    Cancel{" "}
                    <span className="sr-only">{label}</span>
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mt-2 text-body text-muted">
          No upcoming appointments.{" "}
          <Link href="/b" className="text-primary underline">
            Find a business
          </Link>
        </p>
      )}

      <h2 className="mt-8 text-h2 font-semibold">Past</h2>
      <p className="mt-1 text-caption text-muted">Showing the last {PAST_DAYS} days.</p>
      {past.data?.next_cursor && (
        <p className="mt-2 text-body">
          You have more than 50 appointments in the last {PAST_DAYS} days. Only the earliest 50 are shown here.
        </p>
      )}
      {earlier.length ? (
        <ul className="mt-3 space-y-3">
          {earlier.map((item) => {
            const business = at(item);
            return (
              <li key={item.id} className="rounded-md border border-border bg-surface p-4">
                <p className="text-body font-semibold">{describe(item, business)}</p>
                <p className="text-label text-muted">
                  {business.name} · {PAST_STATUS[item.status] ?? item.status}
                </p>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mt-2 text-body text-muted">No appointments in the last {PAST_DAYS} days.</p>
      )}

      {/* Esc closes it natively (= keep); focus goes back to the Cancel button that opened it. */}
      <dialog
        ref={dialog}
        aria-labelledby="cancel-title"
        onClose={() => trigger.current?.isConnected && trigger.current.focus()}
        className="m-auto w-[calc(100%-2rem)] max-w-[420px] rounded-lg border border-border bg-surface p-6 text-text shadow-2 backdrop:bg-black/40"
      >
        <h2 id="cancel-title" className="text-h2 font-semibold">
          Cancel this appointment?
        </h2>
        <p className="mt-2 text-body">{target?.label}</p>
        {error && <Alert>{error}</Alert>}
        <div className="mt-6 flex flex-col gap-3">
          <Button type="button" variant="outline" onClick={() => dialog.current?.close()}>
            Keep it
          </Button>
          <Button type="button" onClick={() => void cancel()} loading={busy}>
            Cancel appointment
          </Button>
        </div>
      </dialog>
    </>
  );
}
