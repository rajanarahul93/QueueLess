"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Alert } from "@/components/Alert";
import { StatusPill } from "@/components/StatusPill";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { AccessEnded } from "@/components/console/AccessEnded";
import { consoleOrder, QueueRow } from "@/components/console/QueueRows";
import { StatusMenu } from "@/components/console/StatusMenu";
import { api, ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { MyBusiness, Queue } from "@/lib/api/types";
import { myBusinessesQuery } from "@/components/console/BusinessPicker";
import { rememberBusiness } from "@/lib/console/remember";
import { useLiveQueue } from "@/lib/stream/useLiveQueue";

const QUEUE_STATUS_TEXT: Record<string, string> = { open: "Open", paused: "Paused", closed: "Closed" };
const QUEUE_NOTE: Record<string, string> = {
  paused: "No new joins or check-ins; you can keep serving.",
  closed: "Closed for today; finish the people still waiting.",
};
/** The clock the waiting minutes are measured against, refreshed on a slow tick. */
const WAIT_TICK_MS = 30_000;

function QueueHeader({
  name,
  queue,
  waiting,
  serving,
  counts,
  pill,
}: {
  name: string;
  queue: { id: string; status: string };
  waiting: number;
  serving: string | null;
  counts: { completed: number; no_show: number; cancelled: number };
  pill: React.ReactNode;
}) {
  return (
    <header>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-h1 font-semibold">{name}</h1>
        {pill}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span
          className="inline-flex h-7 items-center rounded-full px-3 text-label font-medium"
          style={{
            color: `var(--status-${queue.status === "open" ? "completed" : queue.status === "paused" ? "waiting" : "no-show"}-fg)`,
            background: `var(--status-${queue.status === "open" ? "completed" : queue.status === "paused" ? "waiting" : "no-show"}-bg)`,
          }}
        >
          {QUEUE_STATUS_TEXT[queue.status] ?? queue.status}
        </span>
        <Link href="/console" className="min-h-11 px-2 text-label text-primary underline">
          Switch business
        </Link>
      </div>
      <p className="mt-3 text-body">
        <span className="text-muted">Now serving: </span>
        {serving ?? "nobody yet"}
      </p>
      <p className="mt-1 text-label text-muted">
        Completed {counts.completed} · Not here {counts.no_show} · Cancelled {counts.cancelled}
      </p>
      {/* The one spoken summary of the queue; it changes only when the number does. */}
      <p role="status" className="mt-1 text-label text-muted">
        {waiting === 1 ? "1 waiting" : `${waiting} waiting`}
      </p>
      {QUEUE_NOTE[queue.status] && <p className="mt-2 text-body">{QUEUE_NOTE[queue.status]}</p>}
      <div className="mt-4">
        <StatusMenu queueId={queue.id} status={queue.status} />
      </div>
    </header>
  );
}

/** The live part, mounted only once today's queue id is known: the stream never opens on a
 *  half-loaded page, where a request for queue "" would look like lost access. */
function LiveQueue({ queueId, business }: { queueId: string; business: MyBusiness }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), WAIT_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  const { snapshot, status, access, retry } = useLiveQueue(queueId);
  if (access === "ended") return <AccessEnded />;
  if (!snapshot) return <DelayedSkeleton rows={4} />;

  const rows = consoleOrder(snapshot.tokens);
  const serving = rows.find((token) => token.status === "in_service") ?? rows.find((token) => token.status === "called");
  const waiting = snapshot.tokens.filter((token) => token.status === "waiting").length;
  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <QueueHeader
        name={business.name}
        queue={{ id: snapshot.id, status: snapshot.status }}
        waiting={waiting}
        serving={serving ? `#${serving.token_no} ${serving.customer.name}` : null}
        counts={snapshot.counts}
        pill={<StatusPill status={status} onRetry={retry} />}
      />
      {status === "capped" && (
        <p className="mt-3 text-label text-muted">
          Too many open screens. Close another QueueLess tab or window; this one updates every 10 seconds.
        </p>
      )}
      {rows.length === 0 ? (
        <p className="mt-6 text-body text-muted">Queue is empty.</p>
      ) : (
        <>
          <p className="mt-6 text-label text-muted">Order can change when the walk-in ratio applies.</p>
          <ul className="mt-2 space-y-3">
            {rows.map((token) => (
              <QueueRow key={token.id} token={token} zone={business.timezone} now={now} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** The staff console for one business: today's queue, live. Read-mostly in this stage; the only
 *  action is the queue's own status. */
export function QueueConsole({ businessId }: { businessId: string }) {
  const mine = useQuery({ ...myBusinessesQuery, retry: false });
  const business = mine.data?.find((item: MyBusiness) => item.id === businessId);
  // Remembered only once this account's own list confirms the business: a deep link to someone
  // else's business leaves nothing behind for the next visit.
  useEffect(() => {
    if (business) rememberBusiness(business.id);
  }, [business]);
  // Today's queue in the business zone; the API creates it on first read.
  const today = useQuery({
    queryKey: ["queue", "today", businessId],
    queryFn: () => api<Queue>(`/v1/businesses/${businessId}/queue`),
    retry: false,
    staleTime: 60_000,
  });

  const gone = (error: unknown) => error instanceof ApiError && error.status === 404;
  // A member list without this business, or a 404 from it, means access is over.
  if (mine.isError && gone(mine.error)) return <AccessEnded />;
  if (mine.isSuccess && !business) return <AccessEnded />;
  if (today.isError) {
    // A business with no active resource has no queue to show: say so, not an error page.
    return gone(today.error) ? (
      <section className="mx-auto max-w-[640px] px-4 py-8">
        <h1 className="text-h1 font-semibold">{business?.name ?? "Queue console"}</h1>
        <p className="mt-2 text-body text-muted">
          No queue today. This business has no active resource, so nobody can join yet.
        </p>
        <Link href="/console" className="mt-4 inline-block min-h-11 px-2 text-label text-primary underline">
          Switch business
        </Link>
      </section>
    ) : (
      <Alert>{errorMessage(today.error)}</Alert>
    );
  }
  if (!today.data || !business) return <DelayedSkeleton rows={4} />;
  return <LiveQueue queueId={today.data.id} business={business} />;
}
