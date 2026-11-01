import { describe, expect, it } from "vitest";
import { SseParser, type SseEvent } from "@/lib/stream/sse";
import { SSE_EXPECTED, SSE_SAMPLE } from "./sse-sample";

function parse(chunks: Uint8Array[]): { events: SseEvent[]; retry: number | null } {
  const parser = new SseParser();
  const events = chunks.flatMap((chunk) => parser.push(chunk));
  return { events: [...events, ...parser.end()], retry: parser.retry };
}

const bytes = new TextEncoder().encode(SSE_SAMPLE);

describe("SseParser", () => {
  it("reads every framing rule from one chunk", () => {
    expect(parse([bytes])).toEqual({ events: SSE_EXPECTED, retry: 3000 });
  });

  it("gives the same events however the bytes are split (every boundary, UTF-8 and CRLF too)", () => {
    for (let at = 0; at <= bytes.length; at += 1) {
      expect(parse([bytes.slice(0, at), bytes.slice(at)]).events, `split at ${at}`).toEqual(SSE_EXPECTED);
    }
  });

  it("gives the same events one byte at a time", () => {
    expect(parse([...bytes].map((byte) => Uint8Array.of(byte))).events).toEqual(SSE_EXPECTED);
  });

  it("a bare CR at the end of a chunk is one line end, not two, when LF follows", () => {
    const encode = (text: string) => new TextEncoder().encode(text);
    expect(parse([encode("data: a\r"), encode("\n\r\n")]).events).toEqual([{ event: "message", data: "a", id: null }]);
  });
});
