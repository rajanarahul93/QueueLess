"use client";

import { useEffect, useState } from "react";
import type { LiveStatus } from "@/lib/stream/useLiveStream";

/** Live (the word for 3 s after connecting, then a dot), Reconnecting…, Live (slow), Offline.
 *  Shared by the customer ticket and the staff console, so both read the same way. */
export function StatusPill({ status, onRetry }: { status: LiveStatus; onRetry: () => void }) {
  const [fresh, setFresh] = useState(true);
  useEffect(() => {
    if (status !== "live") return;
    const timer = setTimeout(() => setFresh(false), 3000);
    return () => {
      clearTimeout(timer);
      setFresh(true);
    };
  }, [status]);
  if (status === "ended" || status === "gone") return null;
  if (status === "live" && !fresh) {
    return <span role="img" aria-label="Live" data-pill="live" className="inline-block h-2.5 w-2.5 rounded-full bg-[var(--status-completed-fg)]" />;
  }
  const slow = status === "polling" || status === "capped";
  const text = status === "live" ? "Live" : slow ? "Live (slow)" : status === "offline" ? "Offline — retrying" : "Reconnecting…";
  const tone = status === "live" ? "completed" : status === "offline" ? "no-show" : "waiting";
  return (
    <span className="inline-flex items-center gap-2">
      <span
        data-pill={status}
        className="inline-flex h-7 items-center rounded-full px-3 text-label font-medium"
        style={{ color: `var(--status-${tone}-fg)`, background: `var(--status-${tone}-bg)` }}
      >
        {text}
      </span>
      {status === "offline" && (
        <button type="button" onClick={onRetry} className="min-h-11 px-2 text-label text-primary underline">
          Retry now
        </button>
      )}
    </span>
  );
}
