import {extractOnPage} from "./page-extract.js";
import {makeTranscript} from "./transcript.js";
import {failureReport} from "./reports.js";

chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true}).catch(() => {});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !sender.tab?.id) return;
  let page;
  try { page = new URL(sender.tab.url); } catch { return; }
  if (page.origin !== "https://www.youtube.com" || page.pathname !== "/watch") return;
  if (message.type === "probe:selection") {
    // Keep this call in the gesture handler, before any awaited work.
    chrome.sidePanel.open({tabId: sender.tab.id}).catch(() => {});
    return;
  }
  if (message.type !== "probe:extract") return;
  const videoId = page.searchParams.get("v");
  let evidence = {checked_at: new Date().toISOString(), video_id: videoId, response_bytes: null};
  const fail = (code, message) => respond({ok: false, ...failureReport({code, evidence}), message});
  if (videoId !== message.video_id || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) {
    fail("navigation", "Видео изменилось. Повторите проверку.");
    return;
  }
  (async () => {
    try {
      const [execution] = await chrome.scripting.executeScript({
        target: {tabId: sender.tab.id}, world: "MAIN", func: extractOnPage, args: [videoId]
      });
      const extraction = execution?.result;
      evidence = failureReport({evidence: extraction?.evidence ?? evidence}).evidence;
      if (!extraction?.ok) {
        fail(extraction?.code ?? "no-response", extraction?.message ?? "Плеер не вернул данные.");
        return;
      }
      const transcript = await makeTranscript(extraction);
      respond({ok: true, transcript, evidence: {
        ...evidence,
        checked_at: new Date().toISOString(), track_language: extraction.track_language,
        track_id: extraction.track_id, response_bytes: extraction.response_bytes,
        transport: "page-timedtext-json3", normalization: "whitespace-exact-overlap-v1"
      }});
    } catch {
      fail("invalid-transcript", "Не удалось подтвердить полную дорожку. Повторите проверку или импортируйте SRT/VTT в веб-приложении.");
    }
  })();
  return true;
});
