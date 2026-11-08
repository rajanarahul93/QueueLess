"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { StatusPill } from "@/components/StatusPill";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { AccessEnded } from "@/components/console/AccessEnded";
import { myBusinessesQuery } from "@/components/console/BusinessPicker";
import { api, ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { MyBusiness, Queue, SnapshotToken, StaffAppointment, StaffAppointmentPage } from "@/lib/api/types";
import { holdOnChange, PRIMARY_HOLD_MS } from "@/lib/console/actions";
import {
  APPOINTMENT_LABEL,
  CAPPED_NOTICE,
  changedRowNotice,
  checkedInNotice,
  checkInMessage,
  MAX_PAGES,
  PAGE_LIMIT,
  POLL_MS,
  REFETCH_AFTER_DELTA_MS,
  rowAction,
  tokenFor,
  type RowAction,
} from "@/lib/console/appointments";
import { useCheckIn, type CheckedIn } from "@/lib/console/useCheckIn";
import { useLiveQueue } from "@/lib/stream/useLiveQueue";
import { dayRange, timeLabel } from "@/lib/time";

/** Today's appointments for one business, in the BUSINESS zone, read with `from`/`to` as
 *  absolute instants. Pages are followed to the end, with a cap so a server that always
 *  answers with a cursor cannot loop. */
export function appointmentsQuery(businessId: string, zone: string) {
  return {
    queryKey: ["appointments", "today", businessId, zone] as const,
    queryFn: async (): Promise<{ items: StaffAppointment[]; capped: boolean }> => {
      const { from, to } = dayRange(zone);
      const items: StaffAppointment[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const search = new URLSearchParams({ business_id: businessId, from, to, limit: String(PAGE_LIMIT) });
        if (cursor) search.set("cursor", cursor);
        const answer: StaffAppointmentPage = await api<StaffAppointmentPage>(`/v1/appointments?${search}`);
        items.push(...answer.items);
        cursor = answer.next_cursor ?? null;
        if (!cursor) return { items, capped: false };
      }
      return { items, capped: true };
    },
    staleTime: 0,
  };
}

function StatusBadge({ status }: { status: string }) {
  // The queue's own three tones, reused: nothing new is invented for appointments.
  const tone =
    status === "checked_in" || status === "in_service" || status === "completed"
      ? "completed"
      : status === "booked"
        ? "waiting"
        : "no-show";
  return (
    <span
      className="inline-flex h-7 items-center rounded-full px-3 text-label font-medium"
      style={{ color: `var(--status-${tone}-fg)`, background: `var(--status-${tone}-bg)` }}
    >
      {APPOINTMENT_LABEL[status] ?? status}
    </span>
  );
}

/** One appointment. Only the fields named here are ever rendered: the customer's NAME, never
 *  their id, and the response carries no phone or email at all. */
function AppointmentRow({
  appointment,
  token,
  zone,
  businessId,
  action,
  busy,
  slow,
  held,
  onCheckIn,
}: {
  appointment: StaffAppointment;
  token: SnapshotToken | undefined;
  zone: string;
  businessId: string;
  action: RowAction;
  busy: boolean;
  slow: boolean;
  held: boolean;
  onCheckIn: (id: string) => void;
}) {
  return (
    <li className="rounded-md border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-body-lg font-semibold">
          {timeLabel(appointment.start, zone)}–{timeLabel(appointment.end, zone)}
        </span>
        <StatusBadge status={appointment.status} />
      </div>
      <p className="mt-1 text-body">{appointment.customer.name}</p>
      <p className="mt-1 text-label text-muted">{appointment.service_name}</p>
      {token && <p className="mt-1 text-label text-muted">Token #{token.token_no}</p>}
      {action !== "none" && (
        <div className="mt-3">
          {held ? (
            // Someone else's change just renamed this row's action. A click lands on whatever
            // the row says at that moment, so for PRIMARY_HOLD_MS it says it and does nothing.
            <Button disabled>{action === "check-in" ? "Check in" : "Open in queue"}</Button>
          ) : action === "check-in" ? (
            <Button disabled={busy && !slow} loading={busy && !slow} onClick={() => onCheckIn(appointment.id)}>
              Check in
            </Button>
          ) : (
            <Link
              href={`/console/${businessId}?token=${token!.id}`}
              className="inline-flex min-h-12 items-center rounded-md border border-border-strong bg-surface px-4 text-label font-medium"
            >
              Open in queue
            </Link>
          )}
          {busy && slow && <p className="mt-2 text-label text-muted">Still working… check the list.</p>}
        </div>
      )}
    </li>
  );
}

function List({ business, queueId }: { business: MyBusiness; queueId: string }) {
  const { snapshot, status, retry, access } = useLiveQueue(queueId);
  const tokens = snapshot?.tokens ?? [];
  const list = useQuery(appointmentsQuery(business.id, business.timezone));
  const refetch = list.refetch;
  const items = useMemo(() => list.data?.items ?? [], [list.data]);
  const [said, setSaid] = useState("");
  // Which row's action each render last saw, and which rows are held because that action
  // changed under the user's hand. One round per change, so a change inside a running hold
  // restarts it instead of stacking (6D-2's rule, per row).
  const [seen, setSeen] = useState<Record<string, RowAction>>({});
  const [hold, setHold] = useState<{ ids: string[]; round: number }>({ ids: [], round: 0 });
  const [expired, setExpired] = useState(0);
  const holding = hold.round !== 0 && hold.round !== expired;
  const [mine, setMine] = useState<{ id: string; status: string } | null>(null);

  const onToken = useCallback(
    (result: CheckedIn) => {
      const name = items.find((item) => item.id === result.appointmentId)?.customer.name ?? "them";
      setMine({ id: result.appointmentId, status: "checked_in" });
      setSaid(checkedInNotice(name, result.token.token_no, result.created));
      void refetch();
    },
    [items, refetch],
  );
  const { run, busy, slow, error } = useCheckIn(onToken, holding);

  // The action each row offers now, compared with what the last render showed.
  const kinds: Record<string, RowAction> = {};
  for (const item of items) kinds[item.id] = rowAction(item.status, tokenFor(item, tokens) !== undefined);
  // A row this render has not seen before has no entry in `seen`, so it never holds: the rule
  // is 6D-2's, applied per row.
  const theirs = items.filter((item) => {
    const ours = mine !== null && mine.id === item.id && item.status === mine.status;
    return holdOnChange(seen[item.id] ?? null, kinds[item.id], ours);
  });
  const settled =
    Object.keys(kinds).length === Object.keys(seen).length &&
    Object.entries(kinds).every(([id, kind]) => seen[id] === kind);
  if (!settled) {
    setSeen(kinds);
    if (theirs.length > 0) {
      setHold((current) => ({ ids: theirs.map((item) => item.id), round: current.round + 1 }));
      setSaid(changedRowNotice(theirs[0].customer.name, theirs[0].status));
    }
  }
  // setState only in the timer's callback, never in an effect body.
  useEffect(() => {
    if (hold.round === 0 || hold.round === expired) return;
    const round = hold.round;
    const timer = setTimeout(() => setExpired(round), PRIMARY_HOLD_MS);
    return () => clearTimeout(timer);
  }, [hold.round, expired]);

  // Bookings made elsewhere have no event to ride on, so the list is read again on focus and
  // on a slow tick. The tick stops while the tab is hidden: nobody is reading it.
  useEffect(() => {
    const onFocus = () => void refetch();
    const timer = setInterval(() => {
      if (!document.hidden) void refetch();
    }, POLL_MS);
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refetch]);

  // A check-in anywhere arrives as a token delta whose source is "appointment" (the delta
  // carries no appointment id), so the whole list is read again, once a burst has settled.
  const fingerprint = tokens
    .filter((token) => token.source === "appointment")
    .map((token) => `${token.id}:${token.version}`)
    .sort()
    .join(",");
  const lastSeen = useRef<string | null>(null);
  useEffect(() => {
    if (lastSeen.current === null) {
      lastSeen.current = fingerprint; // the first snapshot is what the list was read with
      return;
    }
    if (lastSeen.current === fingerprint) return;
    lastSeen.current = fingerprint;
    const timer = setTimeout(() => void refetch(), REFETCH_AFTER_DELTA_MS);
    return () => clearTimeout(timer);
  }, [fingerprint, refetch]);

  if (access === "ended") return <AccessEnded />;
  if (list.isError && list.error instanceof ApiError && list.error.status === 404) return <AccessEnded reason="missing" />;

  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      {/* The page names itself before its list arrives: a staff member on a slow connection
          still sees where they are, and the back link works at once. */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-h1 font-semibold">Appointments today</h1>
        <StatusPill status={status} onRetry={retry} />
      </div>
      <p className="mt-1 text-body text-muted">{business.name}</p>
      <Link href={`/console/${business.id}`} className="mt-2 inline-block min-h-11 px-2 text-label text-primary underline">
        Back to the queue
      </Link>
      <p role="status" className="mt-2 text-label text-muted">
        {said}
      </p>
      {error !== null && <Alert>{checkInMessage(error)}</Alert>}
      {list.isError && <Alert>{errorMessage(list.error)}</Alert>}
      <p className="mt-2 text-label text-muted">
        Times are {business.timezone}. A booking made somewhere else can take up to 30 seconds to appear here.
      </p>
      {list.data?.capped && <p className="mt-1 text-label text-muted">{CAPPED_NOTICE}</p>}
      {!list.data && !list.isError ? (
        <DelayedSkeleton rows={4} />
      ) : items.length === 0 ? (
        <p className="mt-6 text-body text-muted">No appointments today</p>
      ) : (
        <ul className="mt-4 space-y-3">
          {items.map((item) => (
            <AppointmentRow
              key={item.id}
              appointment={item}
              token={tokenFor(item, tokens)}
              zone={business.timezone}
              businessId={business.id}
              action={kinds[item.id]}
              busy={busy === item.id}
              slow={slow}
              held={holding && hold.ids.includes(item.id)}
              onCheckIn={(id) => void run(id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/** The staff page for one business's appointments today. Access is decided exactly as the
 *  console decides it, so the two screens say the same thing when it ends. */
export function AppointmentsToday({ businessId }: { businessId: string }) {
  const mine = useQuery({ ...myBusinessesQuery, retry: false });
  const business = mine.data?.find((item: MyBusiness) => item.id === businessId);
  const today = useQuery({
    queryKey: ["queue", "today", businessId],
    queryFn: () => api<Queue>(`/v1/businesses/${businessId}/queue`),
    retry: false,
    staleTime: 60_000,
  });

  const gone = (error: unknown) => error instanceof ApiError && error.status === 404;
  if (mine.isError && gone(mine.error)) return <AccessEnded />;
  if (mine.isSuccess && !business) return <AccessEnded reason="missing" />;
  if (today.isError && !gone(today.error)) return <Alert>{errorMessage(today.error)}</Alert>;
  if (today.isError) {
    // No active resource: there is no queue to check anyone into, and no appointment either.
    return (
      <section className="mx-auto max-w-[640px] px-4 py-8">
        <h1 className="text-h1 font-semibold">Appointments today</h1>
        <p className="mt-2 text-body text-muted">
          No queue today. This business has no active resource, so nobody can be checked in yet.
        </p>
        <Link href={`/console/${businessId}`} className="mt-4 inline-block min-h-11 px-2 text-label text-primary underline">
          Back to the queue
        </Link>
      </section>
    );
  }
  if (!business || !today.data) return <DelayedSkeleton rows={4} />;
  return <List business={business} queueId={today.data.id} />;
}
