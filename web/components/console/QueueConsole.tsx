"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { StatusPill } from "@/components/StatusPill";
import { DelayedSkeleton } from "@/components/booking/DelayedSkeleton";
import { AccessEnded } from "@/components/console/AccessEnded";
import { consoleOrder, QueueRow } from "@/components/console/QueueRows";
import { StatusMenu } from "@/components/console/StatusMenu";
import { ActionBar } from "@/components/console/ActionBar";
import { TokenDetail } from "@/components/console/TokenDetail";
import { api, ApiError } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";
import type { MyBusiness, Queue, SnapshotToken } from "@/lib/api/types";
import { actionAnnouncement, changedNotice, endedNotice, holdOnChange, PRIMARY_HOLD_MS, primaryAction, type Primary } from "@/lib/console/actions";
import { useAction, type Press } from "@/lib/console/useAction";
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
  said,
}: {
  name: string;
  queue: { id: string; status: string; businessId: string };
  waiting: number;
  serving: string | null;
  counts: { completed: number; no_show: number; cancelled: number };
  pill: React.ReactNode;
  said: string;
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
        <Link href={`/console/${queue.businessId}/appointments`} className="min-h-11 px-2 text-label text-primary underline">
          Appointments today
        </Link>
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
      {/* The one spoken line: the queue's size, and the outcome of the last action. */}
      <p role="status" className="mt-1 text-label text-muted">
        {said ? `${said}. ` : ""}
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
  const linked = useSearchParams();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), WAIT_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  const { snapshot, status, access, retry, refresh, endedStatusOf } = useLiveQueue(queueId);
  // What this user's own last action produced: a kind change that matches it is theirs, not
  // someone else's. Matching on the state itself needs no arming or disarming.
  const [mine, setMine] = useState<{ id: string; status: string } | null>(null);
  const [seen, setSeen] = useState<{ kind: Primary | null; hold: number; selected: string | null }>({
    kind: null,
    hold: 0,
    selected: null,
  });
  const [expiredHold, setExpiredHold] = useState(0);
  const held = seen.hold !== 0 && seen.hold !== expiredHold;
  const { run, busy, slow, error, release } = useAction(queueId, refresh, held);
  // This tab's own memory: which row is selected, which token it called last, and the one line
  // the staff member reads. None of it is stored anywhere.
  // "Open in queue" on the appointments page links here with ?token=; the router's own reader
  // is what sees it, because during a client navigation the browser's URL is updated after the
  // new page has rendered. Read once: a later deselect must stick.
  const [selectedId, setSelectedId] = useState<string | null>(() => linked.get("token"));
  const [lastCalledId, setLastCalledId] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const tokens = useMemo(() => snapshot?.tokens ?? [], [snapshot]);
  const selected = tokens.find((token) => token.id === selectedId);
  const lastCalled = tokens.find((token) => token.id === lastCalledId);
  const target = selected ?? lastCalled;
  // Derived from state during render, so no effect has to set state: when the primary button's
  // KIND changes because data arrived, hold it briefly and say what changed. `hold` counts the
  // holds, so a change inside the window restarts it instead of stacking.
  const primary = primaryAction(target);
  if (seen.kind !== primary || seen.selected !== selectedId) {
    // Ours when our own action produced exactly this state, or when the user just changed which
    // row is selected. Anything else arrived from the server.
    const ours = (mine !== null && target?.id === mine.id && target?.status === mine.status) || seen.selected !== selectedId;
    setSeen({
      kind: primary,
      selected: selectedId,
      hold: holdOnChange(seen.kind, primary, ours) ? seen.hold + 1 : 0,
    });
    if (!ours && target) setSaid(changedNotice(target.token_no, target.status));
  }
  // setState only in the timer's callback, never in the effect body.
  useEffect(() => {
    if (seen.hold === 0 || seen.hold === expiredHold) return;
    const round = seen.hold;
    const timer = setTimeout(() => {
      release();
      setExpiredHold(round);
    }, PRIMARY_HOLD_MS);
    return () => clearTimeout(timer);
  }, [seen.hold, expiredHold, release]);
  // A token that left the active list takes the panel with it, and is accounted for by name —
  // unless this staff member's own action ended it, which says so in its own words.
  const watched = useRef(new Map<string, SnapshotToken>());
  const accounted = useRef(new Set<string>());
  useEffect(() => {
    const seen = watched.current;
    for (const id of [selectedId, lastCalledId]) {
      const token = id ? tokens.find((item) => item.id === id) : undefined;
      if (id && token) seen.set(id, token);
      else if (id && seen.has(id)) {
        const gone = seen.get(id)!;
        seen.delete(id);
        if (accounted.current.delete(id)) {
          // Our own action already said what it did.
        } else {
          setSaid(endedNotice(gone.token_no, endedStatusOf(id)));
        }
        if (id === selectedId) setSelectedId(null);
        if (id === lastCalledId) setLastCalledId(null);
      }
    }
  }, [tokens, selectedId, lastCalledId, endedStatusOf]);

  async function press(next: Press) {
    const outcome = await run(next);
    if (outcome === "failed") return;
    // Remember what we produced, so the resulting relabel is not read as someone else's change.
    const produced = outcome === "none" ? null : outcome.called;
    setMine(produced ? { id: produced.id, status: produced.status } : null);
    if (next.kind === "call-next") {
      const called = outcome === "none" ? null : outcome.called;
      setLastCalledId(called?.id ?? null);
      if (called) setSelectedId(null); // the bar follows the token it just called
      setSaid(actionAnnouncement("call-next", called?.token_no ?? null));
      return;
    }
    const token = outcome === "none" ? undefined : (outcome.called ?? undefined);
    if (token && !["waiting", "called", "in_service"].includes(token.status)) accounted.current.add(token.id);
    setSaid(actionAnnouncement(next.kind, token?.token_no ?? target?.token_no ?? 0));
  }

  if (access === "ended") return <AccessEnded />;
  if (!snapshot) return <DelayedSkeleton rows={4} />;

  const rows = consoleOrder(snapshot.tokens);
  const serving = rows.find((token) => token.status === "in_service") ?? rows.find((token) => token.status === "called");
  const waiting = snapshot.tokens.filter((token) => token.status === "waiting").length;
  return (
    <section className="mx-auto max-w-[640px] px-4 py-8">
      <QueueHeader
        name={business.name}
        queue={{ id: snapshot.id, status: snapshot.status, businessId: business.id }}
        waiting={waiting}
        serving={serving ? `#${serving.token_no} ${serving.customer.name}` : null}
        counts={snapshot.counts}
        pill={<StatusPill status={status} onRetry={retry} />}
        said={said}
      />
      <ActionBar
        businessId={business.id}
        target={target}
        busy={busy}
        slow={slow}
        held={held}
        error={error}
        onPress={(next) => void press(next)}
      />
      {selected && <TokenDetail token={selected} zone={business.timezone} />}
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
              <QueueRow
                key={token.id}
                token={token}
                zone={business.timezone}
                now={now}
                selected={token.id === selectedId}
                onSelect={(id) => setSelectedId((current) => (current === id ? null : id))}
              />
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
  if (mine.isSuccess && !business) return <AccessEnded reason="missing" />;
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
