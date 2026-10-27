import { test } from "node:test";
import assert from "node:assert/strict";
import { SSEParser, type ServerEvent } from "./sse.ts";

test("handles split CRLF frames, multiple data lines and comments", () => {
  const events: ServerEvent[] = [];
  const parser = new SSEParser((event) => events.push(event));
  parser.feed(": heartbeat\r\nid: 42\r\nevent: snap");
  parser.feed("shot\r\ndata: {\r\ndata: \"ok\": true}\r\n\r");
  assert.equal(events.length, 0);
  parser.feed("\n");
  assert.deepEqual(events, [{ id: "42", event: "snapshot", data: "{\n\"ok\": true}" }]);
});

test("does not emit an incomplete frame or leak IDs across frames", () => {
  const events: ServerEvent[] = [];
  const parser = new SSEParser((event) => events.push(event));
  parser.feed("id: 9\ndata: first\n\ndata: second\n\ndata: incomplete");
  assert.deepEqual(events, [
    { id: "9", event: "message", data: "first" },
    { id: undefined, event: "message", data: "second" },
  ]);
});
