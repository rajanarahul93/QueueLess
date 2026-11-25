"use client";

import { useState } from "react";

/** Demo mode only: no SMS is sent, so the API returns the code and the screen shows it. A status
 *  region (announced politely, never given focus), selectable, with a Copy button. */
export function DemoCode({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      setCopied(false); // blocked clipboard: the code is still selectable on screen
    }
  }

  return (
    <div role="status" className="rounded-sm border border-border-strong bg-surface p-3 text-body">
      <p>
        Demo mode: no SMS is sent. Your code is <strong className="select-all tabular-nums">{code}</strong>
      </p>
      <button
        type="button"
        onClick={() => void copy()}
        className="mt-1 min-h-11 text-label text-primary underline"
      >
        Copy code
      </button>
      {copied && <span className="ml-2 text-label text-muted">Copied</span>}
    </div>
  );
}
