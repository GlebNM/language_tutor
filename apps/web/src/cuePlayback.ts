import type { PlaybackSample } from "./listening";

export type PlaybackCue = { id: string; start_ms: number; end_ms: number };
export type CueAction = { kind: "pause" | "loop"; position: number };

// Owns only navigation and boundary decisions. The player emits the original
// playback sample before applying an action, so listening survives a loop.
export function createCuePlayback(source: readonly PlaybackCue[]) {
  const cues = [...source].sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms);
  let previous: PlaybackSample | undefined;
  let loop: PlaybackCue | null = null;
  let autoPause = false;

  function index(position: number) {
    let found = -1;
    for (let i = 0; i < cues.length && cues[i].start_ms <= position; i++) found = i;
    return found;
  }
  return {
    get loop() { return loop; },
    get autoPause() { return autoPause; },
    current(position: number) { return cues[Math.max(0, index(position))] ?? null; },
    target(position: number, direction: "previous" | "repeat" | "next") {
      const current = index(position);
      const target = direction === "repeat" ? Math.max(0, current)
        : direction === "next" ? current + 1 : current - 1;
      return cues[target] ?? null;
    },
    cancelLoop() { loop = null; },
    setAutoPause(enabled: boolean) {
      autoPause = enabled;
      if (enabled) loop = null;
    },
    toggleLoop(position: number) {
      if (loop) { loop = null; return null; }
      loop = cues[Math.max(0, index(position))] ?? null;
      if (loop) autoPause = false;
      previous = undefined;
      return loop;
    },
    observe(current: PlaybackSample): CueAction | null {
      const before = previous;
      previous = current;
      if (!before || current.discontinuity) return null;
      const advance = current.position - before.position;
      const expected = before.playing ? (current.clock - before.clock) * before.rate : 0;
      const tolerance = Math.max(250, expected * 0.05);
      if (advance < 0 || advance > expected + tolerance) {
        // A seek through YouTube's own controls is not a crossed cue boundary.
        loop = null;
        return null;
      }
      if (!before.playing || advance <= 0) return null;
      if (loop && before.position < loop.end_ms && current.position >= loop.end_ms) {
        return { kind: "loop", position: loop.start_ms };
      }
      if (autoPause) {
        const ended = cues.find(cue => before.position < cue.end_ms && current.position >= cue.end_ms);
        if (ended) return { kind: "pause", position: ended.end_ms - 1 };
      }
      return null;
    },
  };
}

export type PlaybackShortcut = "previous" | "repeat" | "next" | "toggle" | "loop" | "autoPause";
export function playbackShortcut(code: string): PlaybackShortcut | null {
  switch (code) {
    case "KeyA": return "previous";
    case "KeyS": return "repeat";
    case "KeyD": return "next";
    case "Space": return "toggle";
    case "KeyL": return "loop";
    case "KeyQ": return "autoPause";
    default: return null;
  }
}
