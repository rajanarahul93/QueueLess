import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { abortAllStreams } from "@/lib/stream/registry";
import { useLiveTicket, type Ticket } from "@/lib/stream/useLiveTicket";

// A fake API: GET /v1/tokens/t1 hands out a new stream token each time; each stream request
// gets the next scripted answer (a status, or a stream the test writes to).
type Stream = { send: (text: string) => void; close: () => void; aborted: boolean };
type Answer = number | "stream";

let answers: Answer[];
let streams: Stream[];
let connects: { url: string; lastId: string | null }[];
let details: number;
let ticketStatus: string;
let detailStatus: number;

const STATE = { token_id: "t1", token_no: 4, status: "waiting", ahead_count: 2, next: false, eta: { min_minutes: 10, max_minutes: 14 } };
const frame = (event: string, data: unknown, id?: number) => `${id ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const OPEN = "retry: 3000\n\n";

function fakeFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const url = new URL(input);
  const signal = init.signal as AbortSignal;
  if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  if (url.pathname === "/v1/tokens/t1") {
    details += 1;
    if (detailStatus !== 200) return Promise.resolve(new Response(JSON.stringify({ code: "NOT_FOUND" }), { status: detailStatus }));
    const body = { ...STATE, id: "t1", business_id: "b1", queue_id: "q1", status: ticketStatus, source: "walk_in", queued_at: "", joined_at: "2026-11-01T10:00:00+05:30", version: 1, stream_token: `s${details}`, expires_in: 60 };
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
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
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  // A <StrictMode> wrapper does not double-run effects here; the root option does.
  return renderHook(() => useLiveTicket("t1"), { wrapper, reactStrictMode: strict });
}
const ticket = () => client.getQueryData<Ticket>(["ticket", "t1"]);
const tick = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms));
const tokenOf = (index: number) => new URL(connects[index].url).searchParams.get("stream_token");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  answers = [];
  streams = [];
  connects = [];
  details = 0;
  ticketStatus = "waiting";
  detailStatus = 200;
  vi.stubGlobal("fetch", vi.fn(fakeFetch));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function live() {
  const hook = start();
  await tick();
  streams.at(-1)!.send(OPEN + frame("snapshot", STATE, 5));
  await tick();
  expect(hook.result.current.status).toBe("live");
  return hook;
}

describe("useLiveTicket", () => {
  it("applies the snapshot, then deltas; drops older ids and other tokens' events", async () => {
    await live();
    expect(ticket()).toMatchObject({ token_no: 4, ahead_count: 2, business_id: "b1" });
    streams[0].send(frame("token.updated", { ...STATE, ahead_count: 1 }, 6));
    streams[0].send(frame("token.updated", { ...STATE, ahead_count: 9 }, 6)); // not newer
    streams[0].send(frame("token.updated", { ...STATE, token_id: "other", ahead_count: 0 }, 7)); // not ours
    streams[0].send(frame("something.new", { x: 1 }, 8) + ": heartbeat\n\n");
    await tick();
    expect(ticket()?.ahead_count).toBe(1);
  });

  it("shows and clears a running-late notice; being called clears it too", async () => {
    await live();
    streams[0].send(frame("token.running_late", { delay_min: 15, eta: { min_minutes: 20, max_minutes: 30 } }, 6));
    await tick();
    expect(ticket()?.late).toEqual({ delay_min: 15, eta: { min_minutes: 20, max_minutes: 30 } });
    streams[0].send(frame("token.running_late_cleared", { delay_min: 0, eta: null }, 7));
    await tick();
    expect(ticket()?.late).toBeNull();
    streams[0].send(frame("token.running_late", { delay_min: 15, eta: null }, 8));
    streams[0].send(frame("token.updated", { ...STATE, status: "called", ahead_count: null, next: true, eta: null }, 9));
    await tick();
    expect(ticket()).toMatchObject({ status: "called", next: true, late: null });
  });

  it("the server's lifetime end reconnects at once with a NEW stream token and the last id", async () => {
    const hook = await live();
    streams[0].send(frame("token.updated", { ...STATE, ahead_count: 1 }, 12));
    await tick(6000);
    streams[0].close();
    await tick();
    expect(connects).toHaveLength(2);
    expect(tokenOf(1)).not.toBe(tokenOf(0));
    expect(connects[1].lastId).toBe("12");
    expect(hook.result.current.status).toBe("live");
  });

  it("a known-id resume that sends only retry and heartbeats stays Live, with the cache unchanged", async () => {
    const hook = await live();
    await tick(6000);
    streams[0].close();
    await tick();
    streams[1].send(OPEN + ": heartbeat\n\n");
    await tick(25_000);
    streams[1].send(": heartbeat\n\n");
    await tick(25_000);
    expect(hook.result.current.status).toBe("live");
    expect(ticket()?.ahead_count).toBe(2);
    expect(connects).toHaveLength(2);
  });

  it("a clean end within 5 s is a failure: back off 1 s", async () => {
    const hook = await live();
    streams[0].close();
    await tick();
    expect(hook.result.current.status).toBe("reconnecting");
    expect(connects).toHaveLength(1);
    await tick(1000);
    expect(connects).toHaveLength(2);
  });

  it("two failures switch to polling every 10 s, then a stream retry after 60 s goes Live again", async () => {
    answers = [503, 500];
    const hook = start();
    await tick();
    await tick(1000);
    expect(connects).toHaveLength(2);
    expect(hook.result.current.status).toBe("polling");
    const polled = details;
    await tick(10_000);
    expect(details).toBe(polled + 1);
    expect(connects).toHaveLength(2); // no stream while polling
    await tick(50_000);
    expect(connects).toHaveLength(3);
    streams.at(-1)!.send(OPEN + frame("snapshot", STATE, 5));
    await tick();
    expect(hook.result.current.status).toBe("live");
  });

  it("a 401 gets one fresh stream token; a second 401 is a failure", async () => {
    answers = [401, "stream"];
    const hook = start();
    await tick();
    expect(connects).toHaveLength(2);
    expect(tokenOf(1)).not.toBe(tokenOf(0));
    streams[0].send(OPEN);
    await tick();
    expect(hook.result.current.status).toBe("live");
    answers = [401, 401];
    await tick(6000);
    streams[0].close();
    await tick();
    expect(hook.result.current.status).toBe("reconnecting");
  });

  it("404 means the ticket is gone, and nothing reconnects", async () => {
    detailStatus = 404;
    const hook = start();
    await tick(120_000);
    expect(hook.result.current.status).toBe("gone");
    expect(connects).toHaveLength(0);
  });

  it("429 (too many screens) polls at once", async () => {
    answers = [429];
    const hook = start();
    await tick();
    expect(hook.result.current.status).toBe("capped");
  });

  it("a watchdog of 30 s without a byte is a failure", async () => {
    const hook = await live();
    await tick(29_000);
    expect(hook.result.current.status).toBe("live");
    await tick(1000);
    expect(streams[0].aborted).toBe(true);
    expect(hook.result.current.status).toBe("reconnecting");
  });

  it("offline pauses without counting a failure; online reconnects", async () => {
    const hook = await live();
    await act(async () => void window.dispatchEvent(new Event("offline")));
    await tick();
    expect(hook.result.current.status).toBe("offline");
    expect(streams[0].aborted).toBe(true);
    await act(async () => void window.dispatchEvent(new Event("online")));
    await tick();
    expect(connects).toHaveLength(2);
    answers = [500];
    streams[1].send(OPEN);
    await tick(6000);
    streams[1].close(); // one real failure now: still a 1 s backoff, not polling
    await tick();
    expect(hook.result.current.status).toBe("reconnecting");
  });

  it("a tab hidden for 5 minutes closes its stream and reconnects on return, not as a failure", async () => {
    const hook = await live();
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await act(async () => void document.dispatchEvent(new Event("visibilitychange")));
    await tick(5 * 60_000);
    expect(streams[0].aborted).toBe(true);
    hidden.mockReturnValue(false);
    await act(async () => void document.dispatchEvent(new Event("visibilitychange")));
    await tick();
    expect(connects).toHaveLength(2);
    streams[1].send(OPEN);
    await tick();
    expect(hook.result.current.status).toBe("live");
  });

  it("an ended ticket closes the stream and never reconnects", async () => {
    const hook = await live();
    streams[0].send(frame("token.updated", { ...STATE, status: "completed", ahead_count: null, eta: null }, 9));
    await tick(120_000);
    expect(streams[0].aborted).toBe(true);
    expect(hook.result.current.status).toBe("ended");
    expect(connects).toHaveLength(1);
  });

  it("revoked stops streaming and polls instead", async () => {
    const hook = await live();
    streams[0].send(frame("revoked", {}));
    await tick();
    expect(hook.result.current.status).toBe("polling");
  });

  it("unmount and logout abort the stream; logout never reconnects", async () => {
    const first = await live();
    first.unmount();
    expect(streams[0].aborted).toBe(true);
    await live();
    act(() => abortAllStreams());
    await tick(120_000);
    expect(streams[1].aborted).toBe(true);
    expect(connects).toHaveLength(2);
  });

  it("StrictMode opens exactly one stream", async () => {
    start(true);
    await tick(100);
    expect(details).toBe(2); // the effect ran twice
    expect(connects).toHaveLength(1); // but only one stream opened
  });
});
