import assert from "node:assert/strict";
import { test } from "node:test";
import { streamMessage } from "../src/lib/api.ts";

test("handles SSE keep-alives and completes exactly once", async (context) => {
  let request;
  context.mock.method(globalThis, "fetch", async (url, options) => {
    request = { url, options };
    return new Response(': keep-alive\n\ndata: {"type":"token","content":"answer"}\n\ndata: {"type":"done"}\n\n');
  });
  const chunks = [];
  const errors = [];
  await new Promise((resolve) => streamMessage("session", "question", (chunk) => chunks.push(chunk), resolve, (error) => errors.push(error)));
  assert.deepEqual(chunks.map((chunk) => chunk.type), ["token", "done"]);
  assert.deepEqual(errors, []);
  assert.match(request.options.headers["Idempotency-Key"], /^[a-f0-9-]{36}$/);
});

test("surfaces interruption before a terminal event", async (context) => {
  context.mock.method(globalThis, "fetch", async () =>
    new Response('data: {"type":"token","content":"partial"}\n\n'));
  const errors = [];
  await new Promise((resolve) => streamMessage("session", "question", () => {}, resolve, (error) => errors.push(error)));
  assert.deepEqual(errors, ["Connection closed before the response finished."]);
});
