// This function is serialized into MAIN by chrome.scripting: keep it self-contained.
export async function extractOnPage(expectedVideoId) {
  const evidence = {
    checked_at: new Date().toISOString(), video_id: expectedVideoId,
    source: null, track_language: null, http_status: null, response_bytes: null,
    content_type: null, content_length: null
  };
  const fail = (code, message) => ({ok: false, code, message, evidence: {...evidence}});
  const currentId = () => location.pathname === "/watch"
    ? new URL(location.href).searchParams.get("v") : null;
  if (currentId() !== expectedVideoId) return fail("navigation", "Откройте обычную страницу видео YouTube.");

  let response;
  // On SPA navigation the initial response can still belong to the previous video.
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (currentId() !== expectedVideoId) return fail("navigation", "Видео изменилось во время загрузки.");
    try {
      const player = document.getElementById("movie_player");
      const candidates = [player?.getPlayerResponse?.(), window.ytInitialPlayerResponse];
      response = candidates.find(value => value?.videoDetails?.videoId === expectedVideoId);
    } catch { /* Private player interfaces can change; report absence below. */ }
    if (response) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (!response) return fail("player-response-unavailable", "Плеер не отдал данные текущего видео. Повторите проверку или импортируйте SRT/VTT в веб-приложении.");
  if (response.videoDetails.isLive || response.playabilityStatus?.liveStreamability) {
    return fail("live-unsupported", "Прямые эфиры не входят в эту проверку. Откройте обычное записанное видео.");
  }
  const renderer = response.captions?.playerCaptionsTracklistRenderer;
  const available = (renderer?.captionTracks ?? []).filter(track => {
    try {
      const url = new URL(track.baseUrl);
      return track.kind !== "forced" && /^en(?:-|$)/i.test(track.languageCode) && !url.searchParams.has("tlang")
        && url.hostname === "www.youtube.com" && url.pathname === "/api/timedtext"
        && /^en(?:-|$)/i.test(url.searchParams.get("lang") ?? "")
        && url.searchParams.get("v") === expectedVideoId;
    } catch { return false; }
  });
  available.sort((a, b) => Number(a.kind === "asr") - Number(b.kind === "asr"));
  const track = available[0];
  if (!track) return fail("no-english-track", "Исходная английская дорожка недоступна. Используйте импорт SRT/VTT в веб-приложении.");
  evidence.source = track.kind === "asr" ? "youtube-automatic" : "youtube-authored";
  evidence.track_language = typeof track.languageCode === "string" && track.languageCode.length <= 35
    && /^en(?:-[A-Za-z0-9]{1,8})*$/i.test(track.languageCode) ? track.languageCode : null;

  const url = new URL(track.baseUrl);
  url.searchParams.set("fmt", "json3");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const result = await fetch(url.href, {credentials: "same-origin", signal: controller.signal});
    evidence.http_status = result.status;
    const contentType = result.headers.get("content-type");
    evidence.content_type = contentType?.slice(0, 120) ?? null;
    const contentLength = result.headers.get("content-length");
    evidence.content_length = /^\d{1,10}$/.test(contentLength ?? "") ? Number(contentLength) : null;
    if (!result.ok) return fail("timedtext-http", `Сервер субтитров вернул HTTP ${result.status}. Доступен импорт SRT/VTT.`);
    const reader = result.body.getReader();
    const chunks = [];
    let bytes = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      evidence.response_bytes = bytes;
      if (bytes > 2_000_000) {
        await reader.cancel();
        return fail("too-large", "Дорожка превышает предел 2 МБ.");
      }
      chunks.push(value);
    }
    evidence.response_bytes = bytes;
    if (!bytes) return fail("empty-response", "YouTube вернул пустую дорожку. Используйте импорт SRT/VTT.");
    if (currentId() !== expectedVideoId) return fail("navigation", "Видео изменилось во время загрузки.");
    const joined = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.length; }
    return {
      ok: true,
      video_id: expectedVideoId,
      language: "en",
      source: track.kind === "asr" ? "youtube-automatic" : "youtube-authored",
      duration_ms: Number(response.videoDetails.lengthSeconds) * 1000,
      track_language: track.languageCode,
      track_id: track.vssId ?? null,
      response_bytes: bytes,
      evidence: {...evidence},
      body: new TextDecoder("utf-8", {fatal: true}).decode(joined)
    };
  } catch (error) {
    return fail(error.name === "AbortError" ? "timeout" : "network", "Не удалось получить полную дорожку. Проверьте сеть, повторите проверку или импортируйте SRT/VTT.");
  } finally {
    clearTimeout(timeout);
  }
}
