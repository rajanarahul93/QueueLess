"use client";

import { useEffect, useRef, useState } from "react";
import { Alert } from "@/components/Alert";
import { Button } from "@/components/Button";
import { ApiError, newIdempotencyKey, sendKeyed } from "@/lib/api/client";
import { errorMessage } from "@/lib/api/errors";

export const SLOW_PRESS_MS = 5000;

const CHOICES = [
  { status: "open", label: "Open" },
  { status: "paused", label: "Paused" },
  { status: "closed", label: "Closed" },
] as const;

/** Copy for what the server can refuse here; everything else goes through the shared table. */
function statusMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === "INVALID_TRANSITION") {
    return "This queue is closed for today, so its status can't change again.";
  }
  if (error instanceof ApiError && error.code === "FORBIDDEN") {
    return "Only this business's staff and owner can change the queue.";
  }
  if (error instanceof ApiError && error.code === "NOT_FOUND") {
    return "This queue isn't there any more. Reload the console.";
  }
  return errorMessage(error);
}

/** Open / Paused / Closed. No optimistic update: the screen changes when the response (or the
 *  stream's queue.status_changed) says so. Closing is final for the day, so it is confirmed in a
 *  native dialog. One Idempotency-Key per press, reused if the press is retried. */
export function StatusMenu({ queueId, status }: { queueId: string; status: string }) {
  const [pending, setPending] = useState<string | null>(null);
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirming, setConfirming] = useState(false);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const inFlight = useRef(false);
  const keys = useRef(new Map<string, string>());

  useEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (confirming && !node.open) node.showModal();
    if (!confirming && node.open) node.close();
  }, [confirming]);

  // After 5 s the press is still out: say so and let it be pressed again, with the same key.
  useEffect(() => {
    if (pending === null) return;
    const timer = setTimeout(() => setSlow(true), SLOW_PRESS_MS);
    return () => clearTimeout(timer);
  }, [pending]);

  async function change(next: string) {
    if (inFlight.current && !slow) return; // a double press sends one request
    inFlight.current = true;
    setPending(next);
    setSlow(false);
    setError(null);
    // The same press keeps its key, so a retry can never act twice.
    const key = keys.current.get(next) ?? newIdempotencyKey();
    keys.current.set(next, key);
    try {
      await sendKeyed(`/v1/queues/${queueId}`, "PATCH", { status: next }, key);
      keys.current.delete(next); // it landed; the next press is a new action
    } catch (caught) {
      setError(caught);
    } finally {
      inFlight.current = false;
      setPending(null);
    }
  }

  function choose(next: string) {
    if (next === status) return; // the server treats it as a no-op; say nothing alarming
    if (next === "closed") {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setConfirming(true);
      return;
    }
    void change(next);
  }

  return (
    <div>
      <div role="group" aria-label="Queue status" className="flex flex-wrap items-center gap-2">
        {CHOICES.map((choice) => (
          <Button
            key={choice.status}
            variant={choice.status === status ? "primary" : "outline"}
            aria-pressed={choice.status === status}
            disabled={pending !== null && !slow && pending !== choice.status}
            loading={pending === choice.status && !slow}
            onClick={() => choose(choice.status)}
          >
            {choice.label}
          </Button>
        ))}
      </div>
      {slow && <p className="mt-2 text-label text-muted">Still working… check the list.</p>}
      {error !== null && <Alert>{statusMessage(error)}</Alert>}
      <dialog
        ref={dialog}
        onCancel={(event) => {
          event.preventDefault(); // Esc keeps the queue open
          setConfirming(false);
          opener.current?.focus();
        }}
        onClose={() => setConfirming(false)}
        className="max-w-[360px] rounded-md border border-border bg-surface p-4 text-text backdrop:bg-black/40"
        aria-labelledby="close-queue-title"
      >
        <h2 id="close-queue-title" className="text-body-lg font-semibold">
          Close the queue for today?
        </h2>
        <p className="mt-2 text-body text-muted">
          No one else can join or check in, and the queue cannot be reopened today. People already waiting can still be served.
        </p>
        <div className="mt-4 flex justify-end gap-2">
          <Button
            variant="outline"
            onClick={() => {
              setConfirming(false);
              opener.current?.focus();
            }}
          >
            Keep it open
          </Button>
          <Button
            onClick={() => {
              setConfirming(false);
              void change("closed");
            }}
          >
            Yes, close it
          </Button>
        </div>
      </dialog>
    </div>
  );
}
