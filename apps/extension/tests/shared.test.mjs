import {test} from "node:test";
import assert from "node:assert/strict";
import {backendUrl, importPayload, permissionOrigin, timestamp} from "../shared.js";

test("settings accept a local HTTP server and reject remote or credential-bearing URLs", () => {
  assert.equal(backendUrl("http://localhost:8765/"), "http://localhost:8765");
  assert.equal(permissionOrigin("http://127.0.0.1:9000"), "http://127.0.0.1/*");
  for (const value of ["https://example.com", "http://127.0.0.1.evil.test", "http://user:pass@localhost", "http://localhost/api", "http://localhost?secret=x", "file:///tmp/data", "https://localhost"])
    assert.throws(() => backendUrl(value));
});

test("automatic captions enter the shared import contract with original source and full timing", () => {
  const payload = importPayload({video_id:"abcdefghijk",language:"en",source:"youtube-automatic",is_complete:true,
    cues:[{id:"one",start_ms:1250,end_ms:2500,text:"Hello, world."},{id:"two",start_ms:65000,end_ms:67200,text:"Hello again."}]}, "Sample");
  assert.deepEqual(payload, {youtube_url:"https://www.youtube.com/watch?v=abcdefghijk",title:"Sample",format:"srt",language:"en",source:"youtube-automatic",is_complete:true,
    content:"1\n00:00:01,250 --> 00:00:02,500\nHello, world.\n\n2\n00:01:05,000 --> 00:01:07,200\nHello again.\n"});
  assert.equal(timestamp(67200), "1:07");
});
