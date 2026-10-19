import assert from "node:assert/strict";
import { test } from "node:test";

import { api } from "../src/api.ts";

test("a network failure explains how to retry in Russian", async (context) => {
  context.mock.method(globalThis, "fetch", async () => {
    throw new TypeError("Failed to fetch");
  });

  await assert.rejects(
    api("/materials"),
    { message: "Не удалось связаться с сервером. Проверьте подключение и повторите запрос." },
  );
});

test("navigation cancellation keeps the original AbortError", async (context) => {
  const cancellation = new DOMException("The request was aborted", "AbortError");
  context.mock.method(globalThis, "fetch", async () => {
    throw cancellation;
  });

  await assert.rejects(api("/materials"), (error) => error === cancellation);
});

test("the server's validation explanation remains available", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(
    JSON.stringify({ detail: "Дорожка пустая. Добавьте хотя бы одну реплику." }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  ));

  await assert.rejects(
    api("/materials"),
    { message: "Дорожка пустая. Добавьте хотя бы одну реплику." },
  );
});
