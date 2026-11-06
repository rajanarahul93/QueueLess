"use client";

import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { api, ApiError } from "@/lib/api/client";
import { API_URL } from "@/lib/api/config";
import type { Eta, TokenDetail } from "@/lib/api/types";
import type { SseEvent } from "@/lib/stream/sse";
import { parse, useLiveStream, type Connection, type EventControl } from "@/lib/stream/useLiveStream";

export { BACKOFF_MS, HIDDEN_CLOSE_MS, POLL_MS, STREAM_RETRY_MS } from "@/lib/stream/useLiveStream";
export type { LiveStatus } from "@/lib/stream/useLiveStream";

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

/** The customer's live ticket: the shared stream machinery (useLiveStream) pointed at this
 *  token's stream, writing into the shared ["ticket", id] cache. */
export function useLiveTicket(id: string) {
  const queryClient = useQueryClient();
  const ticket = useQuery<Ticket>({ queryKey: ticketKey(id), queryFn: skipToken, staleTime: Infinity }).data;

  const write = useCallback(
    (next: TokenDetail | StreamState) => queryClient.setQueryData<Ticket>(ticketKey(id), (prev) => merge(id, prev, next)),
    [id, queryClient],
  );

  const detail = useCallback(
    async (signal: AbortSignal): Promise<TokenDetail | "gone" | "down" | "aborted"> => {
      try {
        return await api<TokenDetail>(`/v1/tokens/${id}`, { signal });
      } catch (error) {
        if (signal.aborted) return "aborted";
        return error instanceof ApiError && error.status === 404 ? "gone" : "down";
      }
    },
    [id],
  );

  const prepare = useCallback(
    async (signal: AbortSignal): Promise<Connection> => {
      const found = await detail(signal);
      if (found === "aborted") return { kind: "aborted" };
      if (found === "gone") return { kind: "gone" };
      if (found === "down") return { kind: "failed" };
      write(found);
      if (ENDED.includes(found.status)) return { kind: "ended" };
      return { kind: "stream", url: `${API_URL}/v1/tokens/${id}/stream?stream_token=${encodeURIComponent(found.stream_token)}` };
    },
    [detail, id, write],
  );

  const poll = useCallback(
    async (signal: AbortSignal) => {
      const found = await detail(signal);
      if (typeof found === "string") return found;
      write(found);
      return ENDED.includes(found.status) ? ("ended" as const) : ("ok" as const);
    },
    [detail, write],
  );

  const onEvent = useCallback(
    (event: SseEvent, control: EventControl) => {
      if (event.event === "snapshot" || event.event === "token.updated") {
        if (event.event === "token.updated" && control.isOld(event.id)) return;
        const data = parse(event.data);
        if (!data || data.token_id !== id) return; // never another token's state
        control.mark(event.id);
        write(data as unknown as StreamState);
        if (ENDED.includes(String(data.status))) control.stop("ended");
      } else if (event.event === "token.running_late" || event.event === "token.running_late_cleared") {
        if (control.isOld(event.id)) return;
        const data = parse(event.data);
        if (!data) return;
        control.mark(event.id);
        const late = event.event === "token.running_late" ? { delay_min: Number(data.delay_min), eta: (data.eta as Eta) ?? null } : null;
        queryClient.setQueryData<Ticket>(ticketKey(id), (prev) => (prev ? { ...prev, late: prev.status === "waiting" ? late : null } : prev));
      } else if (event.event === "revoked") {
        control.stop("revoked"); // not sent to customers today; if it comes, stop streaming and poll
      } // reset (a snapshot follows), heartbeats and unknown events change nothing
    },
    [id, queryClient, write],
  );

  const { status, retry } = useLiveStream({ prepare, poll, onEvent, deps: [id, queryClient] });
  return { ticket, status, retry };
}
