import assert from "node:assert/strict";
import { test } from "node:test";
import { setSession, sessionFetch, stopSession, learnerStorageKey } from "../src/session.ts";

test("an outgoing write keeps its original session binding when the browser account changes", async context => {
  setSession({user:{id:"alice",username:"Alice"},session_key:"alice-session"});
  let finish;
  let captured;
  context.mock.method(globalThis, "fetch", async (_url, options) => {
    captured = options;
    return new Promise(resolve => {finish = resolve;});
  });
  const write = sessionFetch("/api/materials", {method:"POST",body:"{}"});
  setSession({user:{id:"bob",username:"Bob"},session_key:"bob-session"});
  finish(new Response("{}"));
  await write;
  assert.equal(captured.headers.get("X-Session-Key"), "alice-session");
  assert.equal(captured.credentials, "same-origin");
});

test("a stopped session cannot send pending learning writes", async context => {
  const network = context.mock.method(globalThis, "fetch", async () => new Response("{}"));
  setSession({user:{id:"alice",username:"Alice"},session_key:"alice-session"});
  stopSession();
  await assert.rejects(sessionFetch("/api/data/reset", {method:"POST",body:"{}"}), /Сессия изменилась/);
  assert.equal(network.mock.callCount(), 0);
  await sessionFetch("/api/auth/session");
  assert.equal(network.mock.callCount(), 1);
});

test("saved conversation and playback preferences are scoped to the learner", () => {
  setSession({user:{id:"alice",username:"Alice"},session_key:"alice-session"});
  const alice = learnerStorageKey("last-chat");
  setSession({user:{id:"bob",username:"Bob"},session_key:"bob-session"});
  assert.notEqual(learnerStorageKey("last-chat"), alice);
  setSession({user:{id:"alice",username:"Alice"},session_key:"fresh-session"});
  assert.equal(learnerStorageKey("last-chat"), alice);
});
