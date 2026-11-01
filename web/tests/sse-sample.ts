// One stream that uses every framing rule the parser must handle, and what it must yield.
// Shared by the unit test and the in-browser test (Chromium, Firefox, WebKit).
export const SSE_SAMPLE =
  "retry: 3000\n\n" +
  ": heartbeat\r\n\r\n" +
  'id: 7\r\nevent: snapshot\r\ndata: {"a":1}\r\n\r\n' +
  "id: 8\revent: token.updated\rdata: line one\rdata:line two\r\r" +
  "data: café ☕ ünïcode\n\n" +
  "event: something.new\ndata: x\nid: 9\n\n" +
  "data: never dispatched without a blank line";

export const SSE_EXPECTED = [
  { event: "snapshot", data: '{"a":1}', id: "7" },
  { event: "token.updated", data: "line one\nline two", id: "8" },
  { event: "message", data: "café ☕ ünïcode", id: "8" },
  { event: "something.new", data: "x", id: "9" },
];
