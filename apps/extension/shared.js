export const DEFAULT_BACKEND = "http://127.0.0.1:8765";
export const scores = ["Не знаю", "Учу", "Узнаю в контексте", "Уверенно знаю"];

export function backendUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error("Укажите локальный адрес, например http://127.0.0.1:8765."); }
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname)
      || url.username || url.password || url.pathname !== "/" || url.search || url.hash)
    throw new Error("Поддерживается только HTTP на localhost или 127.0.0.1 без пути и пароля.");
  return url.origin;
}
export const permissionOrigin = value => `${new URL(backendUrl(value)).protocol}//${new URL(value).hostname}/*`;
export const timestamp = milliseconds => {
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
};
const srtTime = milliseconds => {
  const ms = Math.round(milliseconds);
  return `${String(Math.floor(ms/3600000)).padStart(2,"0")}:${String(Math.floor(ms/60000)%60).padStart(2,"0")}:${String(Math.floor(ms/1000)%60).padStart(2,"0")},${String(ms%1000).padStart(3,"0")}`;
};
export function importPayload(transcript, title) {
  return {
    youtube_url: `https://www.youtube.com/watch?v=${transcript.video_id}`,
    title, format: "srt", language: "en", source: transcript.source, is_complete: transcript.is_complete,
    content: transcript.cues.map((cue, index) => `${index+1}\n${srtTime(cue.start_ms)} --> ${srtTime(cue.end_ms)}\n${cue.text}\n`).join("\n"),
  };
}
export function knowledgeLabel(knowledge) {
  if (knowledge?.ignored) return "Исключено из оценки";
  return knowledge?.score == null ? "Пока не оценено" : scores[knowledge.score];
}

export function videoVocabulary(cues) {
  const entries = new Map();
  for (const cue of cues ?? []) for (const token of cue.tokens ?? []) {
    const key = token.lexeme_id ?? `${token.lemma}\u0000${token.part_of_speech}`;
    let entry = entries.get(key);
    if (!entry) {
      entry = {id:key, lexeme_id:token.lexeme_id ?? token.id, lemma:token.lemma, part_of_speech:token.part_of_speech,
        forms:new Set(), count:0, knowledge:token.knowledge ?? null, occurrences:[]};
      entries.set(key, entry);
    }
    entry.forms.add(token.surface);
    entry.count++;
    if (token.knowledge) entry.knowledge=token.knowledge;
    entry.occurrences.push({cue_id:cue.id, token_id:token.id, start_ms:cue.start_ms, text:cue.text, surface:token.surface});
  }
  return [...entries.values()].map(entry => ({...entry, forms:[...entry.forms], occurrences:entry.occurrences}));
}

export function orderVideoVocabulary(entries) {
  const scoreOrder = score => score == null ? -1 : score;
  return [...entries].sort((a,b) => scoreOrder(a.knowledge?.score)-scoreOrder(b.knowledge?.score)
    || b.count-a.count || a.lemma.localeCompare(b.lemma) || a.part_of_speech.localeCompare(b.part_of_speech));
}

export async function api(path, options = {}) {
  const settings = await chrome.storage.local.get({backend: DEFAULT_BACKEND});
  const base = backendUrl(settings.backend);
  if (!await chrome.permissions.contains({origins:[permissionOrigin(base)]}))
    throw new Error("Разрешите доступ к локальному серверу в настройках панели.");
  let response;
  try {
    response = await fetch(`${base}/api${path}`, {
      ...options,
      headers: {...(options.body ? {"Content-Type":"application/json"} : {}), ...options.headers},
      credentials: "omit",
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("Локальный сервер недоступен. Проверьте Docker Compose и адрес в настройках.");
  }
  let body;
  try { body = await response.json(); } catch { throw new Error("Сервер вернул непонятный ответ."); }
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message ?? "Не удалось выполнить запрос.");
  }
  return body;
}
