#!/usr/bin/env node
// Inspect a locally saved full JSON3 response without installing the extension.
import {readFile} from "node:fs/promises";
import {makeTranscript} from "./transcript.js";

const [video_id, source, file, duration] = process.argv.slice(2);
if (!file || !duration || !Number.isFinite(Number(duration)) || Number(duration) <= 0) {
  console.error("Usage: node apps/extension-probe/inspect-json3.mjs VIDEO_ID youtube-authored|youtube-automatic FILE DURATION_MS");
  process.exit(2);
}
try {
  const transcript = await makeTranscript({ok: true, video_id, source,
    duration_ms: Number(duration), body: await readFile(file, "utf8")});
  console.log(JSON.stringify(transcript, null, 2));
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
