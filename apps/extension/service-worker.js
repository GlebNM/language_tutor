import {extractOnPage} from "./page-extract.js";
import {makeTranscript} from "./transcript.js";
import {api, backendUrl, DEFAULT_BACKEND, importPayload} from "./shared.js";

chrome.sidePanel.setPanelBehavior({openPanelOnActionClick:true}).catch(() => {});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !sender.tab?.id) return;
  let page;
  try { page = new URL(sender.tab.url); } catch { return; }
  if (page.origin !== "https://www.youtube.com" || page.pathname !== "/watch") return;
  if (message.type === "lt:selection") {
    chrome.sidePanel.open({tabId:sender.tab.id}).catch(() => {});
    return;
  }
  if (message.type !== "lt:load") return;
  const videoId = page.searchParams.get("v");
  if (videoId !== message.video_id || !/^[A-Za-z0-9_-]{11}$/.test(videoId ?? "")) {
    respond({ok:false,message:"Видео изменилось. Повторите загрузку."});
    return;
  }
  (async () => {
    let evidence = null;
    let stage = "saved-material";
    let backendOrigin = null;
    try {
      const settings = await chrome.storage.local.get({backend:DEFAULT_BACKEND});
      backendOrigin = backendUrl(settings.backend);
      if (message.backend_origin && backendUrl(message.backend_origin) !== backendOrigin)
        throw new Error("Адрес локального сервера изменился. Повторите подключение.");
      const requestApi = (path, options) => api(path, options, backendOrigin);
      if (!message.force_extract) {
        const materials = await requestApi("/materials");
        const saved = materials.find(item => item.youtube_video_id === videoId);
        if (saved) {
          respond({ok:true,material:await requestApi(`/materials/${encodeURIComponent(saved.id)}`),backend_origin:backendOrigin,message:"Открыт сохранённый материал общего профиля."});
          return;
        }
      }
      stage = "page-extraction";
      const [execution] = await chrome.scripting.executeScript({
        target:{tabId:sender.tab.id}, world:"MAIN", func:extractOnPage, args:[videoId],
      });
      const result = execution?.result;
      if (!result?.ok) {
        respond({ok:false,extraction_code:result?.code ?? "extraction-failed",message:result?.message?.replaceAll("в веб-приложении", "в этой панели") ?? "Не удалось извлечь дорожку. Импортируйте SRT/VTT.",evidence:result?.evidence,backend_origin:backendOrigin});
        return;
      }
      evidence = result.evidence ?? null;
      stage = "transcript-validation";
      const transcript = await makeTranscript(result);
      stage = "video-validation";
      const currentTab = await chrome.tabs.get(sender.tab.id);
      if (new URL(currentTab.url).searchParams.get("v") !== videoId) {
        respond({ok:false,extraction_code:"navigation",message:"Видео изменилось. Дорожка предыдущего видео отброшена.",evidence,backend_origin:backendOrigin});
        return;
      }
      stage = "backend-save";
      const summary = await requestApi("/materials", {method:"POST", body:JSON.stringify(importPayload(transcript, String(message.title ?? "").slice(0,200)))});
      respond({ok:true,material:await requestApi(`/materials/${encodeURIComponent(summary.id)}`), evidence:{...result.evidence,transcript_hash:transcript.transcript_hash},backend_origin:backendOrigin,message:"Полная английская дорожка сохранена в общем профиле."});
    } catch (error) {
      respond({ok:false,extraction_code:stage,message:error.message ?? "Повторите загрузку или импортируйте SRT/VTT.",evidence,backend_origin:backendOrigin});
    }
  })();
  return true;
});
