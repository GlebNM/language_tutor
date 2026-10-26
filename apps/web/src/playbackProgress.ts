import type { PlaybackSample } from "./listening";

export type PlaybackSpan = { started_at: string; ended_at: string };

export function createPlaybackTracker(wallOrigin: number) {
  let previous: PlaybackSample | undefined;
  let currentSpan: { start: number; end: number } | null = null;
  const completed: PlaybackSpan[] = [];
  function finish() {
    if (currentSpan && currentSpan.end > currentSpan.start) {
      completed.push({
        started_at: new Date(currentSpan.start).toISOString(),
        ended_at: new Date(currentSpan.end).toISOString(),
      });
    }
    currentSpan = null;
  }
  return {
    sample(current: PlaybackSample) {
      const before = previous;
      previous = current;
      if (!before) return;
      const elapsed = current.clock - before.clock;
      const advance = current.position - before.position;
      const expected = elapsed * before.rate;
      const continuous = !current.discontinuity && before.playing && before.audible &&
        current.audible && before.rate > 0 && elapsed > 0 && elapsed <= 300_000 && advance > 0 &&
        Math.abs(advance - expected) <= Math.max(100, expected * 0.025);
      if (!continuous) { finish(); return; }
      // Count wall time at the selected speed; a frozen/buffering player adds none.
      const duration = Math.min(elapsed, advance / before.rate);
      const end = wallOrigin + current.clock;
      const start = end - duration;
      if (currentSpan && start - currentSpan.end <= 2 && end - currentSpan.start <= 300_000) {
        currentSpan.end = end;
      } else {
        finish(); currentSpan = { start, end };
      }
    },
    drain(): PlaybackSpan[] {
      finish();
      return completed.splice(0);
    },
  };
}
