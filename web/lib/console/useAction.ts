"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, newIdempotencyKey, sendKeyed } from "@/lib/api/client";
import type { QueueSnapshot, SnapshotToken } from "@/lib/api/types";
import { queueKey } from "@/lib/stream/useLiveQueue";

export const SLOW_PRESS_MS = 5000;

/** One press: what to send, and what it is called in the polite line. */
export type Press =
  | { kind: "call-next" }
  | { kind: "start"; tokenId: string; serviceId: string | null }
  | { kind: "complete" | "skip" | "no-show"; tokenId: string }
  | { kind: "priority"; tokenId: string; priority: number };

type Outcome = { called: SnapshotToken | null } | "none" | "failed";

/** The staff action bar's one door to the API.
 *
 *  - the version always comes from the freshest cached token at the moment of the press, never
 *    from a prop captured when the row rendered;
 *  - one Idempotency-Key per deliberate press, kept while the body stays the same, so the 5 s
 *    retry cannot act twice; a different body (a refreshed version after a conflict, another
 *    service) is a different press and gets a new key;
 *  - nothing is optimistic: the response is written into the cache, and the stream's delta plus
 *    the throttled snapshot refetch settle the rest;
 *  - one request at a time (a guard, not just a disabled button); after 5 s the bar re-enables
 *    and says so, and that press keeps its key. */
export function useAction(queueId: string, refresh: () => Promise<void>, blocked = false) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const stamps = useRef(new Map<string, string>());
  const alive = useRef(true);
  // Read inside run(), never during render, so the guard sees the current value.
  const holding = useRef(blocked);
  useEffect(() => {
    holding.current = blocked;
  }, [blocked]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!busy) return;
    const timer = setTimeout(() => setSlow(true), SLOW_PRESS_MS);
    return () => clearTimeout(timer);
  }, [busy]);

  /** The token as the cache now has it: the deltas have already been applied to it. */
  const freshest = useCallback(
    (tokenId: string): SnapshotToken | undefined =>
      queryClient.getQueryData<QueueSnapshot>(queueKey(queueId))?.tokens.find((token) => token.id === tokenId),
    [queryClient, queueId],
  );

  const writeToken = useCallback(
    (token: SnapshotToken) => {
      queryClient.setQueryData<QueueSnapshot>(queueKey(queueId), (prev) => {
        if (!prev) return prev;
        const others = prev.tokens.filter((item) => item.id !== token.id);
        const active = ["waiting", "called", "in_service"].includes(token.status);
        return { ...prev, tokens: active ? [...others, token] : others };
      });
    },
    [queryClient, queueId],
  );

  const run = useCallback(
    async (press: Press): Promise<Outcome> => {
      // Two separate guards: one request at a time, and nothing at all while the primary
      // button is held because someone else's change renamed it. The disabled attribute is the
      // visible half; this is the half that still holds if a click reaches the handler anyway.
      if (holding.current) return "failed";
      if (inFlight.current && !slow) return "failed"; // a double press sends one request
      const target = press.kind === "call-next" ? undefined : freshest(press.tokenId);
      if (press.kind !== "call-next" && !target) {
        setError(new ApiError(404, "NOT_FOUND", null, {}));
        return "failed";
      }
      const version = target?.version;
      // The key identifies this press by what it will send: a changed body is a new press.
      const identity =
        press.kind === "call-next"
          ? "call-next"
          : press.kind === "priority"
            ? `priority:${press.tokenId}:${version}:${press.priority}`
            : press.kind === "start"
              ? `start:${press.tokenId}:${version}:${press.serviceId ?? "-"}`
              : `${press.kind}:${press.tokenId}:${version}`;
      const stamp = stamps.current.get(identity) ?? newIdempotencyKey();
      stamps.current.set(identity, stamp);

      inFlight.current = true;
      setBusy(true);
      setSlow(false);
      setError(null);
      try {
        if (press.kind === "call-next") {
          const called = await sendKeyed<SnapshotToken | undefined>(`/v1/queues/${queueId}/call-next`, "POST", undefined, stamp);
          stamps.current.delete(identity);
          // 204: nobody is waiting. The client gets no body, which is not an error.
          if (!called) return "none";
          if (alive.current) writeToken(called);
          return { called };
        }
        const path =
          press.kind === "priority" ? `/v1/tokens/${press.tokenId}/priority` : `/v1/tokens/${press.tokenId}/${press.kind}`;
        const body =
          press.kind === "priority"
            ? { priority: press.priority, version }
            : press.kind === "start" && press.serviceId
              ? { version, service_id: press.serviceId }
              : { version };
        const updated = await sendKeyed<SnapshotToken>(path, press.kind === "priority" ? "PATCH" : "POST", body, stamp);
        stamps.current.delete(identity);
        if (alive.current) writeToken(updated);
        return { called: updated };
      } catch (caught) {
        if (alive.current) setError(caught);
        // A conflict means this screen was behind: refresh it, and never retry by itself.
        if (caught instanceof ApiError && ["VERSION_CONFLICT", "INVALID_TRANSITION", "NOT_FOUND"].includes(caught.code)) {
          await refresh();
        }
        return "failed";
      } finally {
        inFlight.current = false;
        if (alive.current) setBusy(false);
      }
    },
    [freshest, queueId, refresh, slow, writeToken],
  );

  // Called where the hold timer fires: the guard lets go in the same task that lets the button go,
  // not in the passive effect after the render, so a click in between is sent, not dropped.
  const release = useCallback(() => {
    holding.current = false;
  }, []);

  return { run, busy, slow, error, clearError: () => setError(null), freshest, release };
}
