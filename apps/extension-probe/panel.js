import {failureReport} from "./reports.js";

let tabId;
let snapshot;
let refreshGeneration = 0;
const el = id => document.getElementById(id);
const sourceLabel = {"youtube-authored": "Авторская английская", "youtube-automatic": "Автоматическая английская"};
const time = milliseconds => `${Math.floor(milliseconds / 60000)}:${String(Math.floor(milliseconds / 1000) % 60).padStart(2, "0")}`;
function render(state) {
  snapshot = state;
  el("status").textContent = state.message;
  el("metadata").replaceChildren();
  const transcript = state.transcript;
  const failure = state.status === "error" && state.evidence ? failureReport(state) : null;
  const rows = [["Видео", state.video_id ?? "Нет видео"]];
  if (transcript) rows.push(
    ["Источник", sourceLabel[transcript.source]], ["Язык", state.evidence.track_language],
    ["Полнота ответа", transcript.is_complete ? "Полная дорожка JSON3" : "Не подтверждена"],
    ["Реплик", String(transcript.cues.length)], ["SHA-256", transcript.transcript_hash],
    ["Проверено", state.evidence.checked_at]
  );
  if (state.status === "error") rows.push(["Код ошибки", failureReport(state).code]);
  if (failure) rows.push(
    ["Источник", sourceLabel[failure.evidence.source] ?? "Неизвестен"],
    ["Язык дорожки", failure.evidence.track_language ?? "Неизвестен"],
    ["HTTP-статус", failure.evidence.http_status === null ? "Неизвестен" : String(failure.evidence.http_status)],
    ["Прочитано байт", failure.evidence.response_bytes === null ? "Неизвестно" : String(failure.evidence.response_bytes)],
    ["Проверено", failure.evidence.checked_at ?? "Неизвестно"]
  );
  for (const [label, value] of rows) {
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    el("metadata").append(dt, dd);
  }
  const selected = state.selection;
  el("word").textContent = selected?.word ?? "Нажмите слово поверх плеера.";
  el("cue").textContent = selected?.text ?? "";
  el("occurrence").textContent = selected ? `${time(selected.start_ms)}–${time(selected.end_ms)} · ${selected.occurrence_id}` : "";
  el("export").disabled = !transcript && !failure;
  el("export").textContent = failure ? "Скачать отчёт об ошибке JSON" : "Скачать результат JSON";
  el("start").disabled = state.status === "loading";
}
async function refresh() {
  const request = ++refreshGeneration;
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (request !== refreshGeneration) return;
  tabId = tab?.id;
  try {
    const state = await chrome.tabs.sendMessage(tabId, {type: "probe:get"});
    if (request === refreshGeneration) render(state);
  } catch {
    if (request === refreshGeneration) render({status: "idle", message: "Откройте youtube.com/watch. После установки расширения обновите страницу YouTube."});
  }
}
chrome.runtime.onMessage.addListener((message, sender) => {
  if (sender.id === chrome.runtime.id && sender.tab?.id === tabId
      && ["probe:state", "probe:selection"].includes(message.type)) render(message.state);
});
chrome.tabs.onActivated.addListener(refresh);
chrome.tabs.onUpdated.addListener((id, info) => { if (id === tabId && (info.url || info.status === "complete")) refresh(); });
for (const action of ["start", "stop"]) el(action).addEventListener("click", async () => {
  try { await chrome.tabs.sendMessage(tabId, {type: `probe:${action}`}); await refresh(); }
  catch { render({status: "error", code: "extension-unavailable", message: "Расширение недоступно в этой вкладке. Откройте или обновите страницу YouTube."}); }
});
el("export").addEventListener("click", () => {
  if (!snapshot?.transcript && !(snapshot?.status === "error" && snapshot.evidence)) return;
  const failed = !snapshot.transcript;
  const output = failed ? failureReport(snapshot)
    : {transcript: snapshot.transcript, evidence: snapshot.evidence, selection: snapshot.selection};
  const url = URL.createObjectURL(new Blob([JSON.stringify(output, null, 2)], {type: "application/json"}));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `caption-probe-${failed ? output.evidence.video_id ?? "unknown" : snapshot.video_id}${failed ? "-error" : ""}.json`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
refresh();
