export type PlaybackSample = {
  position: number;
  clock: number;
  playing: boolean;
  audible: boolean;
  rate: number;
  discontinuity?: boolean;
};

type TimedCue = { id: string; start_ms: number; end_ms: number };

// Only a continuous, audible traversal of the entire cue counts. Sampling the
// player's state as well as its position distinguishes playback from seeking.
export function createListeningTracker(cues: TimedCue[]) {
  let previous: PlaybackSample | undefined;
  const started = new Set<string>();
  const completed = new Set<string>();
  return {
    sample(current: PlaybackSample): string[] {
      const before = previous;
      previous = current;
      if (!before || current.discontinuity) {
        started.clear();
        return [];
      }
      const advance = current.position - before.position;
      if (!before.playing && Math.abs(advance) <= 40) return [];
      const elapsed = current.clock - before.clock;
      const expected = elapsed * before.rate;
      // Background tabs can deliver samples seconds apart. Compare against real
      // elapsed time instead of treating each timer tick as heard audio.
      const tolerance = Math.max(220, expected * 0.025);
      const continuous = before.playing && before.audible && current.audible &&
        elapsed >= 0 && advance >= 0 &&
        Math.abs(advance - expected) <= tolerance;
      if (!continuous) {
        started.clear();
        return [];
      }
      const heard: string[] = [];
      for (const cue of cues) {
        if (completed.has(cue.id)) continue;
        if (before.position <= cue.start_ms + 40 && current.position >= cue.start_ms) {
          started.add(cue.id);
        }
        if (started.has(cue.id) && current.position >= cue.end_ms) {
          completed.add(cue.id);
          started.delete(cue.id);
          heard.push(cue.id);
        }
      }
      return heard;
    },
  };
}
