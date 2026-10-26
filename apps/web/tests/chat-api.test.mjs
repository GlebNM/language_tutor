import assert from "node:assert/strict";
import { test } from "node:test";
import { streamReply } from "../src/chatApi.ts";

test("a split UTF-8 stream preserves reply text and the final result", async context => {
  const payload = new TextEncoder().encode('data: {"type":"text","text":"Привет"}\n\ndata: {"type":"complete","response":{"answer":"Привет"}}\n\n');
  context.mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) {
      for (let index = 0; index < payload.length; index += 3) controller.enqueue(payload.slice(index, index + 3));
      controller.close();
    },
  }), {headers: {"Content-Type": "text/event-stream"}}));
  const events = [];
  await streamReply("conversation", {message: "Hello", request_id: "stream-123"}, event => events.push(event));
  assert.deepEqual(events, [
    {type: "text", text: "Привет"}, {type: "complete", response: {answer: "Привет"}},
  ]);
});

test("an interrupted reply can be retried instead of looking complete", async context => {
  context.mock.method(globalThis, "fetch", async () => new Response('data: {"type":"text","text":"Hello"}\n\n'));
  await assert.rejects(streamReply("conversation", {message: "Hello", request_id: "stream-123"}, () => {}),
    {message: "Ответ прерван. Повторите запрос."});
});
