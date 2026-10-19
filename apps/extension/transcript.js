const compact = value => JSON.stringify(value);
const normalize = value => value.replace(/\s+/gu, " ").trim();
const digest = async value => Array.from(new Uint8Array(await crypto.subtle.digest(
  "SHA-256", new TextEncoder().encode(value)
)), byte => byte.toString(16).padStart(2, "0")).join("");

// A successful full timedtext response is required. Visible player text is never input.
export async function makeTranscript(extraction) {
  if (!extraction.ok) throw new Error(extraction.message);
  if (!/^[A-Za-z0-9_-]{11}$/.test(extraction.video_id)) throw new Error("Некорректный ID видео.");
  if (!Number.isFinite(extraction.duration_ms) || extraction.duration_ms <= 0) throw new Error("Длительность записанного видео недоступна.");
  if (new TextEncoder().encode(extraction.body).byteLength > 2_000_000) throw new Error("Дорожка превышает предел 2 МБ.");
  if (!["youtube-authored", "youtube-automatic"].includes(extraction.source)) throw new Error("Неизвестный источник дорожки.");
  let body;
  try { body = JSON.parse(extraction.body); }
  catch { throw new Error("Сервер вернул неизвестный формат вместо JSON3. Доступен импорт SRT/VTT."); }
  if (!Array.isArray(body.events)) throw new Error("В ответе нет списка реплик. Доступен импорт SRT/VTT.");
  const cues = [];
  for (const event of body.events) {
    if (!Array.isArray(event.segs)) continue; // JSON3 also contains window/style events.
    if (event.segs.some(segment => typeof segment.utf8 !== "string")) throw new Error("Неизвестный формат текста реплики.");
    const text = normalize(event.segs.map(segment => segment.utf8).join(""));
    if (!text) continue; // Roll-up newline events carry no spoken words.
    const start = event.tStartMs;
    const duration = event.dDurationMs;
    if (!Number.isInteger(start) || start < 0 || !Number.isInteger(duration) || duration <= 0) {
      throw new Error("У непустой реплики нет допустимого времени. Полнота дорожки не подтверждена.");
    }
    const end = start + duration;
    if (Number.isFinite(extraction.duration_ms) && end > extraction.duration_ms + 1000) {
      throw new Error("Реплика выходит за длительность видео. Проверьте соответствие дорожки.");
    }
    const previous = cues.at(-1);
    if (previous && start < previous.start_ms) throw new Error("Реплики пришли в неверном порядке.");
    // Collapse only exact repeated roll-up text in the same overlapping window.
    // Identical speech later in the video remains a separate occurrence.
    if (extraction.source === "youtube-automatic" && previous && previous.text === text
        && previous.end_ms > start && previous.window_id === event.wWinId) {
      previous.end_ms = Math.max(previous.end_ms, end);
    } else {
      cues.push({start_ms: start, end_ms: end, text, window_id: event.wWinId});
    }
    if (cues.length > 10000) throw new Error("Дорожка содержит больше 10 000 реплик.");
  }
  if (!cues.length) throw new Error("Дорожка пуста. Доступен импорт SRT/VTT.");
  if (cues.reduce((count, cue) => count + Array.from(cue.text).length, 0) > 200000) {
    throw new Error("Дорожка содержит больше 200 000 символов текста.");
  }
  // Key order matches Python json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False).
  const canonicalCues = cues.map(({end_ms, start_ms, text}) => ({end_ms, start_ms, text}));
  const transcript_hash = await digest(compact({cues: canonicalCues, language: "en", video_id: extraction.video_id}));
  const stableCues = await Promise.all(canonicalCues.map(async (cue, index) => ({
    id: `cue-${index + 1}-${(await digest(compact(cue))).slice(0, 16)}`,
    start_ms: cue.start_ms, end_ms: cue.end_ms, text: cue.text
  })));
  return {
    video_id: extraction.video_id, language: "en", source: extraction.source,
    is_complete: true, transcript_hash, cues: stableCues
  };
}
