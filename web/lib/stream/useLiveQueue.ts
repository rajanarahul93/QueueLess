"use client";

import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api/client";
import { API_URL } from "@/lib/api/config";
import type { QueueSnapshot, SnapshotToken, StreamToken } from "@/lib/api/types";
import type { SseEvent } from "@/lib/stream/sse";
import { parse, useLiveStream, type Connection, type EventControl } from "@/lib/stream/useLiveStream";

/** A delta carries the token's own fields, but neither `order_position` (it depends on the whole
 *  queue) nor the counts, so a snapshot is refetched after a burst of them. */
export const REFETCH_EVERY_MS = 2000;

export const queueKey = (id: string) => ["queue", id] as const;

const TOKEN_EVENTS = [
  "token.joined",
  "token.called",
  "token.started",
  "token.completed",
  "token.skipped",
  "token.no_show",
  "token.cancelled",
  "token.priority_changed",
];
/** A token that has left the active list. */
const GONE = ["completed", "no_show", "cancelled"];

/** The fields a staff delta brings; everything else on the row comes from the snapshot. */
type Delta = {
  token_id: string;
  queue_id?: string;
  token_no: number;
  status: string;
  priority: number;
  version: number;
  source: string;
  skip_count: number;
  joined_at: string | null;
  queued_at: string | null;
  called_at: string | null;
  started_at: string | null;
  service_id: string | null;
  service_name: string | null;
  customer?: { id: string; name: string } | null;
};

function applyDelta(snapshot: QueueSnapshot, data: Delta): QueueSnapshot {
  const tokens = snapshot.tokens.filter((token) => token.id !== data.token_id);
  if (GONE.includes(data.status)) return { ...snapshot, tokens };
  const before = snapshot.tokens.find((token) => token.id === data.token_id);
  const row: SnapshotToken = {
    id: data.token_id,
    token_no: data.token_no,
    status: data.status,
    priority: data.priority,
    version: data.version,
    source: data.source,
    skip_count: data.skip_count,
    joined_at: data.joined_at ?? before?.joined_at ?? "",
    queued_at: data.queued_at ?? before?.queued_at ?? "",
    called_at: data.called_at ?? null,
    started_at: data.started_at ?? null,
    // A delta cannot know the call order; the refetch below fills it in.
    order_position: data.status === "waiting" ? (before?.order_position ?? null) : null,
    appointment_id: before?.appointment_id ?? null,
    service_id: data.service_id ?? null,
    service_name: data.service_name ?? null,
    customer: data.customer ?? before?.customer ?? { id: "", name: "" },
  };
  return { ...snapshot, tokens: [...tokens, row] };
}

export type QueueAccess = "ok" | "ended";

/** The staff console's live queue: a stream token from the API, the staff stream, and the
 *  snapshot in ["queue", id]. Deltas are applied at once so a row moves immediately; because a
 *  delta carries no call order and no counts, at most one snapshot refetch runs every 2 s
 *  (trailing, so a burst gives one refetch after the burst, never overlapping). `revoked`, or a
 *  404 from any staff call, ends access to the business. */
export function useLiveQueue(queueId: string) {
  const queryClient = useQueryClient();
  const snapshot = useQuery<QueueSnapshot>({ queryKey: queueKey(queueId), queryFn: skipToken, staleTime: Infinity }).data;
  const [access, setAccess] = useState<QueueAccess>("ok");
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const refetching = useRef(false);
  const stopped = useRef(false);

  useEffect(() => {
    stopped.current = false;
    return () => {
      stopped.current = true;
      clearTimeout(refetchTimer.current);
    };
  }, [queueId]);

  const read = useCallback(
    async (signal?: AbortSignal): Promise<QueueSnapshot | "gone" | "down" | "aborted"> => {
      try {
        return await api<QueueSnapshot>(`/v1/queues/${queueId}`, { signal });
      } catch (error) {
        if (signal?.aborted) return "aborted";
        return error instanceof ApiError && error.status === 404 ? "gone" : "down";
      }
    },
    [queueId],
  );

  const write = useCallback(
    (next: QueueSnapshot) => queryClient.setQueryData<QueueSnapshot>(queueKey(queueId), next),
    [queryClient, queueId],
  );

  /** One refetch per 2 s window, after the burst. Never two at once. */
  const scheduleRefetch = useCallback(() => {
    if (refetchTimer.current !== undefined) return;
    refetchTimer.current = setTimeout(async () => {
      refetchTimer.current = undefined;
      if (stopped.current || refetching.current) return;
      refetching.current = true;
      try {
        const fresh = await read();
        if (fresh === "gone") setAccess("ended");
        else if (typeof fresh !== "string") write(fresh);
      } finally {
        refetching.current = false;
      }
    }, REFETCH_EVERY_MS);
  }, [read, write]);

  const prepare = useCallback(
    async (signal: AbortSignal): Promise<Connection> => {
      const first = await read(signal);
      if (first === "aborted") return { kind: "aborted" };
      if (first === "gone") {
        setAccess("ended");
        return { kind: "gone" };
      }
      if (first === "down") return { kind: "failed" };
      write(first);
      try {
        // A fresh stream token for every connection; it is short-lived by design.
        const issued = await api<StreamToken>(`/v1/queues/${queueId}/stream-token`, { method: "POST", signal });
        return { kind: "stream", url: `${API_URL}/v1/queues/${queueId}/stream?stream_token=${encodeURIComponent(issued.stream_token)}` };
      } catch (error) {
        if (signal.aborted) return { kind: "aborted" };
        if (error instanceof ApiError && error.status === 404) {
          setAccess("ended");
          return { kind: "gone" };
        }
        return { kind: "failed" };
      }
    },
    [queueId, read, write],
  );

  const poll = useCallback(
    async (signal: AbortSignal) => {
      const fresh = await read(signal);
      if (fresh === "gone") {
        setAccess("ended");
        return "gone" as const;
      }
      if (typeof fresh === "string") return fresh;
      write(fresh);
      return "ok" as const;
    },
    [read, write],
  );

  const onEvent = useCallback(
    (event: SseEvent, control: EventControl) => {
      if (event.event === "revoked") {
        setAccess("ended");
        control.stop("ended"); // no reconnect: access to this business is over
        return;
      }
      if (event.event === "snapshot" || event.event === "reset") {
        control.mark(event.id);
        if (event.event === "snapshot") {
          const data = parse(event.data);
          if (data) write(data as unknown as QueueSnapshot);
        } else {
          scheduleRefetch(); // reset: the ids restart, so replace from the API
        }
        return;
      }
      if (!TOKEN_EVENTS.includes(event.event) && event.event !== "queue.status_changed") return;
      if (control.isOld(event.id)) return;
      const envelope = parse(event.data);
      const data = envelope?.data as Record<string, unknown> | undefined;
      if (!envelope || !data) return;
      if (envelope.queue_id !== undefined && envelope.queue_id !== null && envelope.queue_id !== queueId) return; // another queue
      control.mark(event.id);
      if (event.event === "queue.status_changed") {
        const to = data.to;
        queryClient.setQueryData<QueueSnapshot>(queueKey(queueId), (prev) => (prev && typeof to === "string" ? { ...prev, status: to } : prev));
        return;
      }
      queryClient.setQueryData<QueueSnapshot>(queueKey(queueId), (prev) => (prev ? applyDelta(prev, data as unknown as Delta) : prev));
      scheduleRefetch();
    },
    [queryClient, queueId, scheduleRefetch, write],
  );

  const { status, retry } = useLiveStream({ prepare, poll, onEvent, deps: [queueId, queryClient] });
  return { snapshot, status, access, retry };
}
