import assert from "node:assert/strict";
import { test } from "node:test";
import { createListeningTracker } from "../src/listening.ts";

const cues = [
  { id: "first", start_ms: 0, end_ms: 2000 },
  { id: "second", start_ms: 3000, end_ms: 5000 },
  { id: "third", start_ms: 6000, end_ms: 8000 },
];
const sample = (position, clock, changes = {}) => ({
  position, clock, playing: true, audible: true, rate: 1, ...changes,
});

test("pauses and buffering preserve a partially heard cue without adding time", () => {
  const tracker = createListeningTracker(cues);
  tracker.sample(sample(0, 0));
  assert.deepEqual(tracker.sample(sample(1000, 1000, { playing: false })), []);
  tracker.sample(sample(1000, 15000, { playing: false }));
  tracker.sample(sample(1000, 16000));
  assert.deepEqual(tracker.sample(sample(2000, 17000)), ["first"]);
  assert.deepEqual(tracker.sample(sample(2100, 17100)), []);
});

test("accelerated background playback traverses full cues even with sparse samples", () => {
  const tracker = createListeningTracker(cues);
  tracker.sample(sample(0, 0, { rate: 2 }));
  assert.deepEqual(tracker.sample(sample(8000, 4000, { rate: 2 })), ["first", "second", "third"]);
});

test("seeking forward or backward never counts the skipped segment", () => {
  const tracker = createListeningTracker(cues);
  tracker.sample(sample(0, 0));
  tracker.sample(sample(900, 900));
  assert.deepEqual(tracker.sample(sample(4500, 1000)), []);
  assert.deepEqual(tracker.sample(sample(5000, 1500)), []);
  tracker.sample(sample(3000, 1600, { discontinuity: true }));
  assert.deepEqual(tracker.sample(sample(5000, 3600)), ["second"]);
});

test("muting part of a cue requires hearing it again from its beginning", () => {
  const tracker = createListeningTracker(cues);
  tracker.sample(sample(0, 0));
  tracker.sample(sample(900, 900));
  tracker.sample(sample(1200, 1200, { audible: false }));
  tracker.sample(sample(1300, 1300));
  assert.deepEqual(tracker.sample(sample(2000, 2000)), []);
  tracker.sample(sample(0, 2200, { discontinuity: true }));
  assert.deepEqual(tracker.sample(sample(2000, 4200)), ["first"]);
});

test("opening in the middle, frozen playback and a long gap do not imply listening", () => {
  const tracker = createListeningTracker(cues);
  tracker.sample(sample(1000, 0));
  assert.deepEqual(tracker.sample(sample(2000, 1000)), []);
  tracker.sample(sample(3000, 2000));
  tracker.sample(sample(3000, 20000));
  assert.deepEqual(tracker.sample(sample(5000, 22000)), ["second"]);
  assert.deepEqual(tracker.sample(sample(8000, 23000, { playing: false })), []);
});
