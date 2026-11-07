"use client";

import { Fragment } from "react";
import type { SnapshotToken } from "@/lib/api/types";
import { PRIORITY_LABEL } from "@/lib/console/actions";
import { timeLabel } from "@/lib/time";

const STATUS_TEXT: Record<string, string> = { waiting: "Waiting", called: "Called", in_service: "In service" };

/** The selected token, field by field. Only what is listed here is ever shown: no phone, no
 *  email, no ids, whatever else the token object happens to carry. */
export function TokenDetail({ token, zone }: { token: SnapshotToken; zone: string }) {
  const times: [string, string | null][] = [
    ["Joined", token.joined_at],
    ["Called", token.called_at],
    ["Started", token.started_at],
  ];
  return (
    <section aria-label={`Token #${token.token_no}`} className="mt-4 rounded-md border border-border-strong bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-body-lg font-semibold">#{token.token_no}</h2>
        <span className="text-label text-muted">{STATUS_TEXT[token.status] ?? token.status}</span>
      </div>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-label">
        <dt className="text-muted">Customer</dt>
        <dd>{token.customer.name}</dd>
        <dt className="text-muted">Source</dt>
        <dd>{token.source === "appointment" ? "Appointment" : "Walk-in"}</dd>
        <dt className="text-muted">Service</dt>
        <dd>{token.service_name ?? "Service not chosen yet"}</dd>
        <dt className="text-muted">Priority</dt>
        <dd>{PRIORITY_LABEL[token.priority] ?? token.priority}</dd>
        <dt className="text-muted">Skipped</dt>
        <dd>{token.skip_count}×</dd>
        {times.map(([label, value]) =>
          value ? (
            <Fragment key={label}>
              <dt className="text-muted">{label}</dt>
              <dd>
                {timeLabel(value, zone)} ({zone})
              </dd>
            </Fragment>
          ) : null,
        )}
      </dl>
    </section>
  );
}
