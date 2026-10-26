import assert from "node:assert/strict";
import { test } from "node:test";
import { createPlaybackTracker } from "../src/playbackProgress.ts";

const origin = Date.parse("2026-10-22T12:00:00Z");
const sample = (position, clock, extra = {}) => ({ position, clock, playing: true, audible: true, rate: 1, ...extra });
const elapsed = spans => spans.reduce((sum, span) => sum + Date.parse(span.ended_at) - Date.parse(span.started_at), 0);

test("actual minutes include replays and use wall time at 2x speed", () => {
  const tracker = createPlaybackTracker(origin);
  tracker.sample(sample(0, 0, { rate: 2 }));
  tracker.sample(sample(20_000, 10_000, { rate: 2 }));
  assert.equal(elapsed(tracker.drain()), 10_000);
  assert.deepEqual(tracker.drain(), []);
  tracker.sample(sample(0, 11_000, { discontinuity: true }));
  tracker.sample(sample(5000, 16_000));
  assert.equal(elapsed(tracker.drain()), 5000);
});

test("pauses, buffering, muting and seeking add no idle or skipped time", () => {
  const tracker = createPlaybackTracker(origin);
  tracker.sample(sample(0, 0));
  tracker.sample(sample(1000, 1000, { playing: false }));
  tracker.sample(sample(1000, 60_000, { playing: false }));
  tracker.sample(sample(1000, 61_000));
  tracker.sample(sample(2000, 62_000));
  tracker.sample(sample(2000, 63_000));
  tracker.sample(sample(20_000, 63_100));
  tracker.sample(sample(21_000, 64_100, { audible: false }));
  tracker.sample(sample(22_000, 65_100, { audible: false }));
  tracker.sample(sample(22_000, 66_100));
  tracker.sample(sample(23_000, 67_100));
  assert.equal(elapsed(tracker.drain()), 3000);
});

test("continuous background playback counts despite sparse samples", () => {
  const tracker = createPlaybackTracker(origin);
  tracker.sample(sample(0, 0));
  tracker.sample(sample(120_000, 120_000));
  assert.equal(elapsed(tracker.drain()), 120_000);
  tracker.sample(sample(121_000, 1_200_000));
  assert.deepEqual(tracker.drain(), []);
});
