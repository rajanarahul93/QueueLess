import { SseParser, type SseEvent } from "@/lib/stream/sse";

export const WATCHDOG_MS = 30_000;

/** How one connection ended. `ended`: a clean end after the stream was established (HTTP 200
 *  and its `retry:` line), with how long it lived. `failed`: an error status, a network error,
 *  the watchdog, or an end before it was established (status 0 when there was no response). */
export type StreamEnd =
  | { kind: "ended"; livedMs: number }
  | { kind: "failed"; status: number }
  | { kind: "aborted" };

type Options = {
  lastId: string | null;
  signal: AbortSignal;
  onOpen: () => void;
  onEvent: (event: SseEvent) => void;
};

/** Reads one server-sent event stream with fetch (EventSource cannot send Last-Event-ID or
 *  show a status code). 30 s without a byte, heartbeats included, is a failure; while the tab
 *  is hidden the watchdog waits instead (background timers are throttled and data may pause). */
export async function readStream(url: string, { lastId, signal, onOpen, onEvent }: Options): Promise<StreamEnd> {
  const controller = new AbortController();
  const stop = () => controller.abort();
  signal.addEventListener("abort", stop);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let silent = false;
  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (document.hidden) return arm();
      silent = true;
      controller.abort();
    }, WATCHDOG_MS);
  };
  try {
    if (signal.aborted) return { kind: "aborted" };
    arm();
    const response = await fetch(url, {
      headers: { Accept: "text/event-stream", ...(lastId ? { "Last-Event-ID": lastId } : {}) },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      void response.body?.cancel();
      return { kind: "failed", status: response.status };
    }
    const parser = new SseParser();
    const reader = response.body.getReader();
    let openedAt: number | null = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      arm();
      for (const event of parser.push(value)) onEvent(event);
      if (openedAt === null && parser.retry !== null) {
        openedAt = Date.now();
        onOpen();
      }
    }
    for (const event of parser.end()) onEvent(event);
    return openedAt === null ? { kind: "failed", status: 200 } : { kind: "ended", livedMs: Date.now() - openedAt };
  } catch {
    if (signal.aborted && !silent) return { kind: "aborted" };
    return { kind: "failed", status: 0 };
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", stop);
  }
}
