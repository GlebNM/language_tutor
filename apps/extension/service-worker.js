import {extractOnPage} from "./page-extract.js";
import {makeTranscript} from "./transcript.js";
import {api, importPayload} from "./shared.js";

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
    try {
      if (!message.force_extract) {
        const materials = await api("/materials");
        const saved = materials.find(item => item.youtube_video_id === videoId);
        if (saved) {
          respond({ok:true,material:await api(`/materials/${encodeURIComponent(saved.id)}`),message:"Открыт сохранённый материал общего профиля."});
          return;
        }
      }
      const [execution] = await chrome.scripting.executeScript({
        target:{tabId:sender.tab.id}, world:"MAIN", func:extractOnPage, args:[videoId],
      });
      const result = execution?.result;
      if (!result?.ok) {
        respond({ok:false,message:result?.message?.replaceAll("в веб-приложении", "в этой панели") ?? "Не удалось извлечь дорожку. Импортируйте SRT/VTT.",evidence:result?.evidence});
        return;
      }
      const transcript = await makeTranscript(result);
      const currentTab = await chrome.tabs.get(sender.tab.id);
      if (new URL(currentTab.url).searchParams.get("v") !== videoId) {
        respond({ok:false,message:"Видео изменилось. Дорожка предыдущего видео отброшена."});
        return;
      }
      const summary = await api("/materials", {method:"POST", body:JSON.stringify(importPayload(transcript, String(message.title ?? "").slice(0,200)))});
      respond({ok:true,material:await api(`/materials/${encodeURIComponent(summary.id)}`), evidence:{...result.evidence,transcript_hash:transcript.transcript_hash},message:"Полная английская дорожка сохранена в общем профиле."});
    } catch (error) {
      respond({ok:false,message:error.message ?? "Повторите загрузку или импортируйте SRT/VTT."});
    }
  })();
  return true;
});
