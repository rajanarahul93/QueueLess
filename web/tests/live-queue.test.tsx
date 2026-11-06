import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueueSnapshot } from "@/lib/api/types";
import { abortAllStreams } from "@/lib/stream/registry";
import { queueKey, useLiveQueue } from "@/lib/stream/useLiveQueue";

// A fake API: GET /v1/queues/q1 is the snapshot, POST .../stream-token hands out a new token
// each time, and each stream request gets the next scripted answer.
type Stream = { send: (text: string) => void; close: () => void; aborted: boolean };
type Answer = number | "stream";

let answers: Answer[];
let streams: Stream[];
let connects: { url: string; lastId: string | null }[];
let snapshots: number;
let snapshotStatus: number;
let queueStatus: string;
let tokens: QueueSnapshot["tokens"];

const row = (over: Partial<QueueSnapshot["tokens"][number]> = {}) => ({
  id: "t1",
  token_no: 1,
  status: "waiting",
  priority: 0,
  source: "walk_in",
  skip_count: 0,
  version: 1,
  joined_at: "2026-11-02T09:00:00+05:30",
  queued_at: "2026-11-02T09:00:00+05:30",
  called_at: null,
  started_at: null,
  order_position: 1,
  appointment_id: null,
  service_id: null,
  service_name: null,
  customer: { id: "c1", name: "Asha" },
  ...over,
});

const delta = (over: Record<string, unknown> = {}) => ({
  id: 7,
  type: "token.called",
  queue_id: "q1",
  data: {
    token_id: "t1",
    token_no: 1,
    status: "called",
    priority: 0,
    version: 2,
    source: "walk_in",
    skip_count: 0,
    joined_at: "2026-11-02T09:00:00+05:30",
    queued_at: "2026-11-02T09:00:00+05:30",
    called_at: "2026-11-02T09:05:00+05:30",
    started_at: null,
    service_id: null,
    service_name: null,
    customer: { id: "c1", name: "Asha" },
  },
  ...over,
});

const frame = (event: string, data: unknown, id?: number) => `${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const OPEN = "retry: 3000\n\n";

function fakeFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const url = new URL(input);
  const signal = init.signal as AbortSignal;
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  if (url.pathname === "/v1/queues/q1" && (init.method ?? "GET") === "GET") {
    snapshots += 1;
    if (snapshotStatus !== 200) return Promise.resolve(new Response(JSON.stringify({ code: "NOT_FOUND" }), { status: snapshotStatus }));
    const body: QueueSnapshot = {
      id: "q1",
      business_id: "b1",
      resource_id: "r1",
      queue_date: "2026-11-02",
      status: queueStatus,
      counts: { completed: 0, no_show: 0, cancelled: 0 },
      tokens,
    };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  }
  if (url.pathname === "/v1/queues/q1/stream-token") {
    return Promise.resolve(
      new Response(JSON.stringify({ stream_token: `s${connects.length + 1}`, expires_in: 60 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }
  connects.push({ url: input, lastId: new Headers(init.headers).get("Last-Event-ID") });
  const answer = answers.shift() ?? "stream";
  if (answer !== "stream") return Promise.resolve(new Response("{}", { status: answer }));
  let control!: ReadableStreamDefaultController<Uint8Array>;
  const stream: Stream = {
    send: (text) => control.enqueue(new TextEncoder().encode(text)),
    close: () => control.close(),
    aborted: false,
  };
  const body = new ReadableStream<Uint8Array>({ start: (c) => void (control = c) });
  signal?.addEventListener("abort", () => {
    stream.aborted = true;
    control.error(new DOMException("Aborted", "AbortError"));
  });
  streams.push(stream);
  return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }));
}

let client: QueryClient;
function start(strict = false) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return renderHook(() => useLiveQueue("q1"), { wrapper, reactStrictMode: strict });
}
const snapshot = () => client.getQueryData<QueueSnapshot>(queueKey("q1"));
const tick = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms));
const tokenOf = (index: number) => new URL(connects[index].url).searchParams.get("stream_token");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  answers = [];
  streams = [];
  connects = [];
  snapshots = 0;
  snapshotStatus = 200;
  queueStatus = "open";
  tokens = [row()];
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function live() {
  const hook = start();
  await tick();
  streams.at(-1)!.send(OPEN);
  await tick();
  expect(hook.result.current.status).toBe("live");
  return hook;
}

describe("useLiveQueue", () => {
  it("reads the snapshot into the cache and opens the staff stream", async () => {
    const hook = await live();
    expect(snapshot()?.tokens).toHaveLength(1);
    expect(hook.result.current.snapshot?.status).toBe("open");
    expect(new URL(connects[0].url).pathname).toBe("/v1/queues/q1/stream");
  });

  it("applies a delta at once, service fields included, before any refetch", async () => {
    await live();
    streams[0].send(frame("token.called", delta({ data: { ...delta().data, service_id: "sv1", service_name: "Checkup" } }), 7));
    await tick();
    const [token] = snapshot()!.tokens;
    expect(token.status).toBe("called");
    expect([token.service_id, token.service_name]).toEqual(["sv1", "Checkup"]);
    expect(token.version).toBe(2);
    expect(snapshots).toBe(1); // the row moved without waiting for a refetch
  });

  it("a burst of deltas triggers exactly one snapshot refetch, after the burst", async () => {
    await live();
    const before = snapshots;
    for (let index = 0; index < 5; index += 1) {
      streams[0].send(frame("token.called", delta({ id: 10 + index }), 10 + index));
    }
    await tick();
    expect(snapshots).toBe(before); // nothing yet
    await tick(1999);
    expect(snapshots).toBe(before);
    await tick(1);
    expect(snapshots).toBe(before + 1); // one, for the whole burst
    streams[0].send(frame("token.called", delta({ id: 30 }), 30));
    await tick(2000);
    expect(snapshots).toBe(before + 2); // a later delta gets its own window
  });

  it("ignores an event whose id is at or below the last applied id", async () => {
    await live();
    streams[0].send(frame("token.called", delta({ id: 7 }), 7));
    await tick();
    streams[0].send(frame("token.priority_changed", delta({ id: 7, data: { ...delta().data, priority: 2, status: "waiting" } }), 7));
    await tick();
    expect(snapshot()!.tokens[0].priority).toBe(0);
  });

  it("drops an event for another queue", async () => {
    await live();
    streams[0].send(frame("token.called", delta({ queue_id: "other", id: 8 }), 8));
    await tick();
    expect(snapshot()!.tokens[0].status).toBe("waiting");
  });

  it("a queue.status_changed delta updates the status in place", async () => {
    await live();
    streams[0].send(frame("queue.status_changed", { id: 9, type: "queue.status_changed", queue_id: "q1", data: { queue_id: "q1", from: "open", to: "paused" } }, 9));
    await tick();
    expect(snapshot()?.status).toBe("paused");
  });

  it("reset refetches the snapshot instead of trusting the stream", async () => {
    await live();
    const before = snapshots;
    tokens = [row({ status: "called" })];
    streams[0].send(frame("reset", { reason: "unknown" }, 11));
    await tick(2000);
    expect(snapshots).toBe(before + 1);
    expect(snapshot()!.tokens[0].status).toBe("called");
  });

  it("revoked ends access and stops streaming", async () => {
    const hook = await live();
    streams[0].send(frame("revoked", {}));
    await tick(120_000);
    expect(hook.result.current.access).toBe("ended");
    expect(connects).toHaveLength(1); // no reconnect
  });

  it("a 404 from the snapshot ends access", async () => {
    snapshotStatus = 404;
    const hook = start();
    await tick(120_000);
    expect(hook.result.current.access).toBe("ended");
    expect(hook.result.current.status).toBe("gone");
  });

  it("every connection gets a NEW stream token and resumes from the last id", async () => {
    await live();
    streams[0].send(frame("token.called", delta({ id: 12 }), 12));
    await tick(6000);
    streams[0].close();
    await tick();
    expect(connects).toHaveLength(2);
    expect(tokenOf(1)).not.toBe(tokenOf(0));
    expect(connects[1].lastId).toBe("12");
  });

  it("two failures fall back to polling the snapshot", async () => {
    answers = [503, 500];
    const hook = start();
    await tick();
    await tick(1000);
    expect(hook.result.current.status).toBe("polling");
    const polled = snapshots;
    await tick(10_000);
    expect(snapshots).toBeGreaterThan(polled);
  });

  it("logout aborts the stream and never reconnects", async () => {
    await live();
    act(() => abortAllStreams());
    await tick(120_000);
    expect(streams[0].aborted).toBe(true);
    expect(connects).toHaveLength(1);
  });

  it("StrictMode opens exactly one stream", async () => {
    start(true);
    await tick(100);
    expect(connects).toHaveLength(1);
  });
});
