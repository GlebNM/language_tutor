import assert from "node:assert/strict";
import { test } from "node:test";
import { createCuePlayback, playbackShortcut } from "../src/cuePlayback.ts";
import { createListeningTracker } from "../src/listening.ts";

const cues = [
  { id: "one", start_ms: 1000, end_ms: 2000 },
  { id: "two", start_ms: 3000, end_ms: 4000 },
  { id: "three", start_ms: 4000, end_ms: 6000 },
];
const sample = (position, clock = position, extra = {}) => ({
  position, clock, playing: true, audible: true, rate: 1, ...extra,
});

test("navigation handles the intro, gaps, exact boundaries and end of track", () => {
  const player = createCuePlayback(cues);
  assert.equal(player.target(0, "next")?.id, "one");
  assert.equal(player.target(0, "previous"), null);
  assert.equal(player.target(1500, "repeat")?.start_ms, 1000);
  assert.equal(player.target(2500, "repeat")?.id, "one");
  assert.equal(player.target(2500, "next")?.id, "two");
  assert.equal(player.target(4000, "previous")?.id, "two");
  assert.equal(player.target(8000, "repeat")?.id, "three");
  assert.equal(player.target(8000, "next"), null);
  assert.equal(createCuePlayback([]).target(0, "repeat"), null);
});

test("continuous viewing is the default and manual seeks never trigger auto-pause", () => {
  const player = createCuePlayback(cues);
  player.observe(sample(1000));
  assert.equal(player.observe(sample(2100)), null);
  player.setAutoPause(true);
  assert.equal(player.observe(sample(5000, 2200, { discontinuity: true })), null);
  assert.equal(player.observe(sample(5500, 2700)), null);
});

test("auto-pause holds the completed caption and resumes past it once", () => {
  const player = createCuePlayback(cues);
  player.setAutoPause(true);
  player.observe(sample(1900));
  assert.deepEqual(player.observe(sample(2050)), { kind: "pause", position: 1999 });
  assert.equal(player.observe(sample(2050, 3000, { playing: false })), null);
  assert.equal(player.observe(sample(2050, 5000)), null);
  assert.equal(player.observe(sample(3050, 6000)), null);
  assert.deepEqual(player.observe(sample(4050, 7000)), { kind: "pause", position: 3999 });
});

test("loop and auto-pause are explicit mutually exclusive modes", () => {
  const player = createCuePlayback(cues);
  player.setAutoPause(true);
  assert.equal(player.toggleLoop(3200)?.id, "two");
  assert.equal(player.autoPause, false);
  player.observe(sample(3000, 0, { discontinuity: true }));
  player.observe(sample(3900, 900));
  assert.deepEqual(player.observe(sample(4050, 1050)), { kind: "loop", position: 3000 });
  player.setAutoPause(true);
  assert.equal(player.loop, null);
  assert.equal(player.autoPause, true);
});

test("manual pause and buffering do not restart an armed loop", () => {
  const player = createCuePlayback(cues);
  player.toggleLoop(1500);
  player.observe(sample(1000));
  player.observe(sample(1500, 1500, { playing: false }));
  assert.equal(player.observe(sample(1500, 15000, { playing: false })), null);
  assert.equal(player.observe(sample(1500, 16000)), null);
  assert.deepEqual(player.observe(sample(2000, 16500)), { kind: "loop", position: 1000 });
});

test("native forward/backward seeking cancels a loop without a false boundary", () => {
  for (const destination of [0, 5000]) {
    const player = createCuePlayback(cues);
    player.toggleLoop(1500);
    player.observe(sample(1500));
    assert.equal(player.observe(sample(destination, 1550)), null);
    assert.equal(player.loop, null);
  }
  const paused = createCuePlayback(cues);
  paused.toggleLoop(1500);
  paused.observe(sample(1500, 0, { playing: false }));
  paused.observe(sample(5000, 10000, { playing: false }));
  assert.equal(paused.loop, null);
});

test("overlapping captions have deterministic navigation and ordered stops", () => {
  const player = createCuePlayback([
    { id: "first", start_ms: 0, end_ms: 2000 },
    { id: "overlap", start_ms: 1500, end_ms: 2500 },
  ]);
  assert.equal(player.current(1700)?.id, "overlap");
  player.setAutoPause(true);
  player.observe(sample(1900));
  assert.deepEqual(player.observe(sample(2050)), { kind: "pause", position: 1999 });
  assert.deepEqual(player.observe(sample(2550)), { kind: "pause", position: 2499 });
});

test("full audible cue is counted before loop seek; replay never duplicates it", () => {
  const controls = createCuePlayback(cues);
  const listening = createListeningTracker(cues);
  controls.toggleLoop(1000);
  for (const value of [sample(1000, 0), sample(1900, 900)]) {
    listening.sample(value); controls.observe(value);
  }
  const end = sample(2050, 1050);
  assert.deepEqual(listening.sample(end), ["one"]);
  assert.deepEqual(controls.observe(end), { kind: "loop", position: 1000 });
  const seek = sample(1000, 1051, { discontinuity: true });
  assert.deepEqual(listening.sample(seek), []);
  controls.observe(seek);
  assert.deepEqual(listening.sample(sample(2050, 2101)), []);
});

test("auto-pausing still counts a complete cue at slower and faster speeds", () => {
  for (const rate of [0.5, 2]) {
    const controls = createCuePlayback(cues);
    const listening = createListeningTracker(cues);
    controls.setAutoPause(true);
    const begin = sample(1000, 0, { rate });
    controls.observe(begin); listening.sample(begin);
    const end = sample(2050, 1050 / rate, { rate, playing: false });
    assert.deepEqual(listening.sample(end), ["one"]);
    assert.deepEqual(controls.observe(end), { kind: "pause", position: 1999 });
  }
});

test("reaching the final video frame can loop instead of completing the material", () => {
  const controls = createCuePlayback(cues);
  controls.toggleLoop(4500);
  controls.observe(sample(5900));
  assert.deepEqual(controls.observe(sample(6000, 6000, { playing: false })), { kind: "loop", position: 4000 });
});

test("physical shortcut codes work independently of the keyboard language", () => {
  assert.equal(playbackShortcut("KeyS"), "repeat");
  assert.equal(playbackShortcut("KeyA"), "previous");
  assert.equal(playbackShortcut("KeyD"), "next");
  assert.equal(playbackShortcut("Space"), "toggle");
  assert.equal(playbackShortcut("KeyL"), "loop");
  assert.equal(playbackShortcut("KeyQ"), "autoPause");
  assert.equal(playbackShortcut("Enter"), null);
});
