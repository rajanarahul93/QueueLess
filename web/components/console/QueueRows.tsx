"use client";

import type { SnapshotToken } from "@/lib/api/types";
import { timeLabel } from "@/lib/time";

const STATUS_TEXT: Record<string, string> = { waiting: "Waiting", called: "Called", in_service: "In service" };
const SERVING = ["called", "in_service"];
const PRIORITY_TEXT: Record<number, string> = { 1: "Priority", 2: "Urgent" };

/** Being served first (called, then in service), then the waiting tokens in call order.
 *  `order_position` is the server's own call order, including the walk-in ratio; called and
 *  in-service tokens carry none, so they are kept in the order the server listed them. */
export function consoleOrder(tokens: readonly SnapshotToken[]): SnapshotToken[] {
  const serving = tokens.filter((token) => SERVING.includes(token.status));
  const waiting = tokens
    .filter((token) => !SERVING.includes(token.status))
    .slice()
    .sort((left, right) => (left.order_position ?? Number.MAX_SAFE_INTEGER) - (right.order_position ?? Number.MAX_SAFE_INTEGER));
  return [...serving, ...waiting];
}

/** Minutes a token has been waiting, from when it joined. */
export function waitedMinutes(joinedAt: string, now: number): number {
  const since = new Date(joinedAt).getTime();
  return Number.isFinite(since) ? Math.max(0, Math.floor((now - since) / 60_000)) : 0;
}

function SourceIcon({ source }: { source: string }) {
  const appointment = source === "appointment";
  return (
    <span className="inline-flex items-center gap-1 text-label text-muted">
      <svg aria-hidden="true" viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
        {appointment ? (
          <>
            <rect x="3" y="5" width="18" height="16" rx="2" />
            <path d="M8 3v4M16 3v4M3 11h18" />
          </>
        ) : (
          <>
            <circle cx="12" cy="7" r="3" />
            <path d="M5 21v-2a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v2" />
          </>
        )}
      </svg>
      {appointment ? "Appointment" : "Walk-in"}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone = status === "in_service" ? "completed" : status === "called" ? "called" : "waiting";
  return (
    <span
      className="inline-flex h-7 items-center gap-1 rounded-full px-3 text-label font-medium"
      style={{ color: `var(--status-${tone}-fg)`, background: `var(--status-${tone}-bg)` }}
    >
      <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2">
        {status === "in_service" ? <path d="M5 12l5 5L20 7" /> : status === "called" ? <path d="M12 3v18M5 10l7-7 7 7" /> : <circle cx="12" cy="12" r="8" />}
      </svg>
      {STATUS_TEXT[status] ?? status}
    </span>
  );
}

/** One row. Only the fields listed here are ever rendered: no phone, no email, no ids. */
export function QueueRow({ token, zone, now }: { token: SnapshotToken; zone: string; now: number }) {
  const waited = waitedMinutes(token.joined_at, now);
  return (
    <li className="rounded-md border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-body-lg font-semibold">#{token.token_no}</span>
        <StatusBadge status={token.status} />
      </div>
      <p className="mt-1 text-body">{token.customer.name}</p>
      <p className="mt-1 text-label text-muted">{token.service_name ?? "Service not chosen yet"}</p>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <SourceIcon source={token.source} />
        {PRIORITY_TEXT[token.priority] && <span className="text-label font-medium text-primary">{PRIORITY_TEXT[token.priority]}</span>}
        {token.skip_count > 0 && <span className="text-label text-muted">Skipped {token.skip_count}×</span>}
        <span className="text-label text-muted">
          Waiting {waited} min · joined {timeLabel(token.joined_at, zone)} ({zone})
        </span>
      </div>
    </li>
  );
}
