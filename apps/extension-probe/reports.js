const failureCodes = new Set([
  "navigation", "player-response-unavailable", "live-unsupported", "no-english-track",
  "timedtext-http", "too-large", "empty-response", "timeout", "network", "no-response",
  "invalid-transcript", "extension-unavailable"
]);

// A page result is external data: copy only bounded public fields into error reports.
export function failureReport(state) {
  const evidence = {video_id: state.video_id, ...state.evidence};
  const integer = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max ? value : null;
  const boundedString = (value, pattern, maxLength) => typeof value === "string"
    && value.length <= maxLength && pattern.test(value) ? value : null;
  return {
    status: "error",
    code: failureCodes.has(state.code) ? state.code : "unknown-error",
    evidence: {
      checked_at: boundedString(evidence.checked_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/, 24),
      video_id: boundedString(evidence.video_id, /^[A-Za-z0-9_-]{11}$/, 11),
      source: ["youtube-authored", "youtube-automatic"].includes(evidence.source) ? evidence.source : null,
      track_language: boundedString(evidence.track_language, /^en(?:-[A-Za-z0-9]{1,8})*$/i, 35),
      http_status: integer(evidence.http_status, 100, 599),
      response_bytes: integer(evidence.response_bytes, 0, Number.MAX_SAFE_INTEGER)
    }
  };
}
