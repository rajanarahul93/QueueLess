import { expect, test } from "@playwright/test";
import { SseParser } from "../lib/stream/sse";
import { SSE_EXPECTED, SSE_SAMPLE } from "../tests/sse-sample";

// The parser runs in the browser on that browser's own TextDecoder, so it is checked in
// Chromium, Firefox and WebKit too: whole, split at every byte boundary, and byte by byte.
test("the stream parser gives the same events in this browser however the bytes are split", async ({ page }) => {
  await page.goto("about:blank");
  const results = await page.evaluate(
    ({ source, text }) => {
      const Parser = new Function(`return (${source})`)() as typeof SseParser;
      const bytes = new TextEncoder().encode(text);
      const run = (chunks: Uint8Array[]) => {
        const parser = new Parser();
        const events = chunks.flatMap((chunk) => parser.push(chunk));
        return JSON.stringify([...events, ...parser.end()]);
      };
      const out = [run([bytes]), run([...bytes].map((byte) => Uint8Array.of(byte)))];
      for (let at = 0; at <= bytes.length; at += 1) out.push(run([bytes.slice(0, at), bytes.slice(at)]));
      return out;
    },
    { source: SseParser.toString(), text: SSE_SAMPLE },
  );
  expect(new Set(results)).toEqual(new Set([JSON.stringify(SSE_EXPECTED)]));
});
