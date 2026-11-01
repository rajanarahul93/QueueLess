/** One server-sent event. `id` is the last id the stream has set (it carries over, as in the
 *  EventSource rules), or null if it never set one. */
export type SseEvent = { event: string; data: string; id: string | null };

/** An incremental text/event-stream parser: bytes in, events out. A chunk may end anywhere,
 *  inside a UTF-8 character, a line or a CRLF. Lines end in LF, CRLF or CR; comments (the
 *  server's heartbeat) are skipped; `retry` is recorded, but the caller keeps its own timing. */
export class SseParser {
  lastId: string | null = null;
  retry: number | null = null;
  private decoder = new TextDecoder();
  private buffer = "";
  private data: string[] = [];
  private event = "";

  push(chunk: Uint8Array): SseEvent[] {
    return this.feed(this.decoder.decode(chunk, { stream: true }), false);
  }

  /** The stream ended: a last line without its line break still counts. */
  end(): SseEvent[] {
    return this.feed(this.decoder.decode(), true);
  }

  private feed(text: string, done: boolean): SseEvent[] {
    this.buffer += text;
    const events: SseEvent[] = [];
    let start = 0;
    for (let at = 0; at < this.buffer.length; at += 1) {
      const char = this.buffer[at];
      if (char !== "\n" && char !== "\r") continue;
      if (char === "\r" && at === this.buffer.length - 1 && !done) break; // maybe half a CRLF
      this.line(this.buffer.slice(start, at), events);
      if (char === "\r" && this.buffer[at + 1] === "\n") at += 1;
      start = at + 1;
    }
    this.buffer = this.buffer.slice(start);
    if (done && this.buffer) {
      this.line(this.buffer, events);
      this.buffer = "";
    }
    return events;
  }

  private line(line: string, events: SseEvent[]): void {
    if (line === "") {
      if (this.data.length) events.push({ event: this.event || "message", data: this.data.join("\n"), id: this.lastId });
      this.data = [];
      this.event = "";
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + (line[colon + 1] === " " ? 2 : 1));
    if (field === "data") this.data.push(value);
    else if (field === "event") this.event = value;
    else if (field === "id" && !value.includes("\0")) this.lastId = value;
    else if (field === "retry" && /^\d+$/.test(value)) this.retry = Number(value);
  }
}
