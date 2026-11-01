"use client";

import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api/client";
import { API_URL } from "@/lib/api/config";
import type { Eta, TokenDetail } from "@/lib/api/types";
import { readStream } from "@/lib/stream/read";
import { registerStream } from "@/lib/stream/registry";
import type { SseEvent } from "@/lib/stream/sse";

export type LiveStatus = "connecting" | "live" | "reconnecting" | "polling" | "capped" | "offline" | "ended" | "gone";

/** The ticket every screen reads: the server's state plus when it last changed. */
export type Ticket = {
  id: string;
  business_id: string | null;
  token_no: number;
  status: string;
  ahead_count: number | null;
  next: boolean;
  eta: Eta | null;
  joined_at: string | null;
  late: { delay_min: number; eta: Eta | null } | null;
  updatedAt: number;
};

type StreamState = Pick<Ticket, "token_no" | "status" | "ahead_count" | "next" | "eta"> & { token_id: string };

export const ENDED = ["completed", "no_show", "cancelled"];
export const BACKOFF_MS = [1000, 2000, 4000, 10_000];
export const POLL_MS = 10_000;
export const STREAM_RETRY_MS = 60_000;
export const HIDDEN_CLOSE_MS = 5 * 60_000;
const SHORT_LIFE_MS = 5000; // a clean end sooner than this counts as a failure (no hot loop)

export const ticketKey = (id: string) => ["ticket", id] as const;

function merge(id: string, prev: Ticket | undefined, next: TokenDetail | StreamState): Ticket {
  const changed =
    !prev ||
    prev.status !== next.status ||
    prev.ahead_count !== next.ahead_count ||
    prev.eta?.min_minutes !== next.eta?.min_minutes ||
    prev.eta?.max_minutes !== next.eta?.max_minutes;
  return {
    id,
    business_id: "business_id" in next ? next.business_id : (prev?.business_id ?? null),
    token_no: next.token_no,
    status: next.status,
    ahead_count: next.ahead_count,
    // The REST token has no `next`; this is the server's own rule for the stream's field.
    next: "next" in next ? next.next : next.status === "called" || (next.status === "waiting" && next.ahead_count === 0),
    eta: next.eta,
    joined_at: "joined_at" in next ? next.joined_at : (prev?.joined_at ?? null),
    late: next.status === "waiting" ? (prev?.late ?? null) : null, // called or later: no notice
    updatedAt: changed ? Date.now() : prev.updatedAt,
  };
}

function parse(data: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(data);
    return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

type Outcome = "closed" | "failed" | "unauthorized" | "gone" | "capped" | "ended" | "revoked" | "aborted";

/** The customer's live ticket: one stream (fetch, a fresh stream token for every connection,
 *  Last-Event-ID on reconnects) writing into the shared ["ticket", id] cache, with the
 *  reconnect, polling, offline and hidden-tab rules. Returns the ticket and the connection. */
export function useLiveTicket(id: string) {
  const queryClient = useQueryClient();
  const ticket = useQuery<Ticket>({ queryKey: ticketKey(id), queryFn: skipToken, staleTime: Infinity }).data;
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const retryNow = useRef<() => void>(() => {});

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
    const write = (next: TokenDetail | StreamState) =>
      queryClient.setQueryData<Ticket>(ticketKey(id), (prev) => merge(id, prev, next));
    const isOld = (eventId: string | null) => eventId !== null && lastId !== null && Number(eventId) <= Number(lastId);

    const onEvent = (event: SseEvent) => {
      if (event.event === "snapshot" || event.event === "token.updated") {
        if (event.event === "token.updated" && isOld(event.id)) return;
        const data = parse(event.data);
        if (!data || data.token_id !== id) return; // never another token's state
        lastId = event.id ?? lastId;
        write(data as StreamState);
        if (ENDED.includes(String(data.status))) {
          endedNow = true;
          current?.abort();
        }
      } else if (event.event === "token.running_late" || event.event === "token.running_late_cleared") {
        if (isOld(event.id)) return;
        const data = parse(event.data);
        if (!data) return;
        lastId = event.id ?? lastId;
        const late = event.event === "token.running_late" ? { delay_min: Number(data.delay_min), eta: (data.eta as Eta) ?? null } : null;
        queryClient.setQueryData<Ticket>(ticketKey(id), (prev) => (prev ? { ...prev, late: prev.status === "waiting" ? late : null } : prev));
      } else if (event.event === "revoked") {
        revoked = true; // not sent to customers today; if it comes, stop streaming and poll
        current?.abort();
      } // reset (a snapshot follows), heartbeats and unknown events change nothing
    };

    async function detail(signal: AbortSignal): Promise<TokenDetail | "gone" | "down" | "aborted"> {
      try {
        return await api<TokenDetail>(`/v1/tokens/${id}`, { signal });
      } catch (error) {
        if (signal.aborted) return "aborted";
        return error instanceof ApiError && error.status === 404 ? "gone" : "down";
      }
    }

    async function connect(): Promise<Outcome> {
      const controller = new AbortController();
      current = controller;
      const unregister = registerStream(controller);
      try {
        const found = await detail(controller.signal);
        if (found === "aborted" || found === "gone") return found;
        if (found === "down") return "failed";
        write(found);
        if (ENDED.includes(found.status)) return "ended";
        const url = `${API_URL}/v1/tokens/${id}/stream?stream_token=${encodeURIComponent(found.stream_token)}`;
        const end = await readStream(url, {
          lastId,
          signal: controller.signal,
          onEvent,
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

    async function poll(): Promise<"ok" | "gone" | "down" | "ended" | "aborted"> {
      const controller = new AbortController();
      current = controller;
      const unregister = registerStream(controller);
      try {
        const found = await detail(controller.signal);
        if (typeof found === "string") return found;
        write(found);
        return ENDED.includes(found.status) ? "ended" : "ok";
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
            const result = await poll();
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
  }, [id, queryClient]);

  return { ticket, status, retry: () => retryNow.current() };
}
