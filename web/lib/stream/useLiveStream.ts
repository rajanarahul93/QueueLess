"use client";

import { useEffect, useRef, useState } from "react";
import { readStream } from "@/lib/stream/read";
import { registerStream } from "@/lib/stream/registry";
import type { SseEvent } from "@/lib/stream/sse";

export type LiveStatus = "connecting" | "live" | "reconnecting" | "polling" | "capped" | "offline" | "ended" | "gone";

export const BACKOFF_MS = [1000, 2000, 4000, 10_000];
export const POLL_MS = 10_000;
export const STREAM_RETRY_MS = 60_000;
export const HIDDEN_CLOSE_MS = 5 * 60_000;
const SHORT_LIFE_MS = 5000; // a clean end sooner than this counts as a failure (no hot loop)

/** What one connection attempt prepared: a stream to read, or a reason not to. */
export type Connection =
  | { kind: "stream"; url: string }
  | { kind: "ended" } // nothing more can happen; stop for good
  | { kind: "gone" } // 404: the thing being watched is not there
  | { kind: "failed" } // could not prepare; counts as a failure
  | { kind: "aborted" };

/** What an event handler may do to the connection it is reading. */
export type EventControl = {
  /** True for an event this connection has already applied (replay after a reconnect). */
  isOld: (eventId: string | null) => boolean;
  /** Remember this event's id, so a reconnect resumes after it. */
  mark: (eventId: string | null) => void;
  /** Close the stream: `ended` never reconnects, `revoked` falls back to polling. */
  stop: (reason: "ended" | "revoked") => void;
};

export type LiveStreamOptions = {
  /** Fetch what the stream needs (always a FRESH stream token), write the first state, and
   *  return the URL to read. Runs before every connection, including every reconnect. */
  prepare: (signal: AbortSignal) => Promise<Connection>;
  /** One read while the stream is unavailable. */
  poll: (signal: AbortSignal) => Promise<"ok" | "gone" | "down" | "ended" | "aborted">;
  onEvent: (event: SseEvent, control: EventControl) => void;
  /** Re-open the stream when these change (the effect's dependencies). */
  deps: readonly unknown[];
};

export function parse(data: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(data);
    return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type Outcome = "closed" | "failed" | "unauthorized" | "gone" | "capped" | "ended" | "revoked" | "aborted";

/** One live stream and every rule around it, shared by the customer ticket and the staff queue:
 *  a fresh stream token per connection, Last-Event-ID on reconnects, a clean end reconnects at
 *  once, failures back off 1/2/4/10 s, two failures switch to polling with a 60 s stream retry,
 *  429 caps, offline and hidden tabs pause without counting failures, and logout aborts through
 *  the registry. Returns the connection status and a manual retry. */
export function useLiveStream({ prepare, poll, onEvent, deps }: LiveStreamOptions) {
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const retryNow = useRef<() => void>(() => {});
  // The callers rebuild these every render; the loop below must not restart for that, so it
  // reads them through a ref that this effect keeps current.
  const latest = useRef({ prepare, poll, onEvent });
  useEffect(() => {
    latest.current = { prepare, poll, onEvent };
  }, [prepare, poll, onEvent]);

  useEffect(() => {
    let stopped = false;
    let current: AbortController | null = null;
    let lastId: string | null = null;
    let failures = 0;
    let polling = false;
    let capped = false;
    let pause: "offline" | "hidden" | null = navigator.onLine ? null : "offline";
    let endedNow = false;
    let revoked = false;
    let wake = () => {};
    let hiddenTimer: ReturnType<typeof setTimeout> | undefined;
    const show = (next: LiveStatus) => {
      if (!stopped) setStatus(next);
    };
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const timer = setTimeout(done, ms);
        function done() {
          clearTimeout(timer);
          wake = () => {};
          resolve();
        }
        wake = done;
      });
    const resumed = () =>
      new Promise<void>((resolve) => {
        wake = () => {
          wake = () => {};
          resolve();
        };
      });

    const control: EventControl = {
      isOld: (eventId) => eventId !== null && lastId !== null && Number(eventId) <= Number(lastId),
      mark: (eventId) => {
        lastId = eventId ?? lastId;
      },
      stop: (reason) => {
        if (reason === "ended") endedNow = true;
        else revoked = true;
        current?.abort();
      },
    };

    async function connect(): Promise<Outcome> {
      const controller = new AbortController();
      current = controller;
      const unregister = registerStream(controller);
      try {
        const ready = await latest.current.prepare(controller.signal);
        if (ready.kind !== "stream") return ready.kind;
        const end = await readStream(ready.url, {
          lastId,
          signal: controller.signal,
          onEvent: (event) => latest.current.onEvent(event, control),
          onOpen: () => {
            failures = 0;
            polling = false;
            capped = false;
            show("live");
          },
        });
        if (end.kind === "aborted") return endedNow ? "ended" : revoked ? "revoked" : "aborted";
        if (end.kind === "ended") return end.livedMs < SHORT_LIFE_MS ? "failed" : "closed";
        if (end.status === 401) return "unauthorized";
        if (end.status === 404) return "gone";
        if (end.status === 429) return "capped";
        return "failed";
      } finally {
        unregister();
        if (current === controller) current = null;
      }
    }

    async function pollOnce() {
      const controller = new AbortController();
      current = controller;
      const unregister = registerStream(controller);
      try {
        return await latest.current.poll(controller.signal);
      } finally {
        unregister();
        if (current === controller) current = null;
      }
    }

    async function run() {
      let refused = false; // one fresh stream token after a 401, then it is a failure
      while (!stopped) {
        if (pause) {
          if (pause === "offline") show("offline");
          await resumed();
          if (!stopped && !polling) show("reconnecting");
          continue;
        }
        if (polling) {
          const until = Date.now() + STREAM_RETRY_MS;
          while (!stopped && !pause && Date.now() < until) {
            const result = await pollOnce();
            if (result === "gone") return show("gone");
            if (result === "ended") return show("ended");
            if (result === "aborted") break;
            show(result === "down" ? "offline" : capped ? "capped" : "polling");
            await sleep(Math.max(0, Math.min(POLL_MS, until - Date.now())));
          }
          if (stopped || pause) continue;
        }
        const outcome = await connect();
        if (outcome === "closed") {
          refused = false;
          continue; // the server's lifetime limit: reconnect at once
        }
        if (outcome === "gone") return show("gone");
        if (outcome === "ended") return show("ended");
        if (outcome === "aborted") {
          if (stopped || pause) continue;
          stopped = true; // aborted from outside (logout): never reconnect
          return;
        }
        if (outcome === "unauthorized" && !refused) {
          refused = true;
          continue;
        }
        refused = false;
        if (outcome === "capped" || outcome === "revoked") {
          capped = outcome === "capped";
          polling = true;
          continue;
        }
        failures += 1; // failed, 503, a second 401, or a clean end within 5 s
        if (polling) continue;
        if (failures >= 2) {
          polling = true;
          continue;
        }
        show("reconnecting");
        await sleep(BACKOFF_MS[Math.min(failures - 1, BACKOFF_MS.length - 1)]);
      }
    }

    const onOffline = () => {
      pause = "offline";
      current?.abort();
      wake();
    };
    const onOnline = () => {
      if (pause !== "offline") return;
      pause = null;
      wake();
    };
    const onVisibility = () => {
      clearTimeout(hiddenTimer);
      if (document.hidden) {
        hiddenTimer = setTimeout(() => {
          pause = "hidden"; // closing a hidden tab's stream is not a failure
          current?.abort();
          wake();
        }, HIDDEN_CLOSE_MS);
      } else if (pause === "hidden") {
        pause = null;
        wake();
      }
    };
    retryNow.current = () => {
      if (pause === "offline") pause = null;
      wake();
    };
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisibility);
    void run();
    return () => {
      stopped = true;
      current?.abort();
      wake();
      clearTimeout(hiddenTimer);
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the caller names what reopens it
  }, deps);

  return { status, retry: () => retryNow.current() };
}
