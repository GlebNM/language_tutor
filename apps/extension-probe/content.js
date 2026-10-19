(() => {
  let generation = 0;
  let enabled = false;
  let host;
  let player;
  let lastCueIds = "";
  const videoId = () => location.pathname === "/watch" ? new URL(location.href).searchParams.get("v") : null;
  let currentId = videoId();
  let state = {status: "idle", video_id: currentId, message: "Нажмите «Проверить дорожку»."};

  const publish = (type = "probe:state") => chrome.runtime.sendMessage({type, state}).catch(() => {});
  const detach = () => {
    host?.remove();
    player?.classList.remove("lt-caption-probe-active");
    host = undefined;
    player = undefined;
    lastCueIds = "";
  };
  const reset = message => {
    generation += 1;
    detach();
    state = {status: "idle", video_id: currentId, message};
    publish();
  };
  const start = async () => {
    currentId = videoId();
    reset("Получаем полную английскую дорожку…");
    if (!/^[A-Za-z0-9_-]{11}$/.test(currentId ?? "")) {
      enabled = false;
      state = {...state, status: "error", code: "navigation", message: "Откройте обычную страницу youtube.com/watch."};
      publish();
      return;
    }
    enabled = true;
    const request = generation;
    state = {...state, status: "loading"};
    publish();
    try {
      const result = await chrome.runtime.sendMessage({type: "probe:extract", video_id: currentId});
      if (request !== generation || videoId() !== currentId) return;
      state = result?.ok
        ? {status: "ready", video_id: currentId, transcript: result.transcript, evidence: result.evidence, selection: null,
          message: "Полная дорожка получена. Нажмите слово в реплике."}
        : {status: "error", video_id: currentId, code: result?.code ?? "no-response", evidence: result?.evidence,
          message: result?.message ?? "Расширение не ответило. Повторите проверку."};
    } catch {
      if (request !== generation) return;
      state = {status: "error", video_id: currentId, code: "extension-unavailable",
        evidence: {checked_at: new Date().toISOString(), video_id: currentId, response_bytes: null},
        message: "Расширение перезагружено. Обновите страницу YouTube."};
    }
    publish();
  };

  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id) return;
    if (message.type === "probe:get") { respond(state); return; }
    if (message.type === "probe:start") { start(); respond({ok: true}); return; }
    if (message.type === "probe:stop") {
      enabled = false;
      reset("Слой выключен. Штатные субтитры восстановлены.");
      respond({ok: true});
    }
  });

  function attach(nextPlayer) {
    detach();
    player = nextPlayer;
    player.classList.add("lt-caption-probe-active");
    host = document.createElement("div");
    host.setAttribute("aria-label", "Английские субтитры Language Tutor");
    host.style.cssText = "position:absolute;bottom:15%;left:10%;width:80%;z-index:50;pointer-events:none;text-align:center";
    const shadow = host.attachShadow({mode: "closed"});
    const style = document.createElement("style");
    style.textContent = `
      :host { color:white; font:clamp(17px,2.2vw,29px)/1.5 Arial,sans-serif; }
      p { margin:3px auto; padding:3px 9px; border-radius:5px; width:fit-content; max-width:100%; background:rgba(0,0,0,.86); }
      button { all:unset; display:inline; color:inherit; font:inherit; cursor:pointer; pointer-events:auto; text-decoration:underline; text-underline-offset:4px; }
      button:hover,button:focus-visible { color:#ffe08a; outline:2px solid #ffe08a; outline-offset:2px; }
    `;
    shadow.append(style);
    const container = document.createElement("div");
    shadow.append(container);
    player.append(host);
    return container;
  }
  let container;
  function draw(cues, video) {
    const key = cues.map(cue => cue.id).join("|");
    if (key === lastCueIds) return;
    lastCueIds = key;
    container.replaceChildren();
    for (const cue of cues) {
      const line = document.createElement("p");
      let position = 0;
      for (const match of cue.text.matchAll(/[\p{L}\p{M}]+(?:['’\-][\p{L}\p{M}]+)*/gu)) {
        line.append(document.createTextNode(cue.text.slice(position, match.index)));
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = match[0];
        const start = Array.from(cue.text.slice(0, match.index)).length;
        const end = start + Array.from(match[0]).length;
        button.setAttribute("aria-label", `Разобрать ${match[0]}, ${Math.floor(cue.start_ms / 1000)} секунда`);
        button.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          if (!enabled || state.video_id !== videoId()) return;
          video.pause();
          state = {...state, selection: {
            occurrence_id: `${cue.id}:chars:${start}:${end}`, cue_id: cue.id,
            start_char: start, end_char: end, word: match[0], text: cue.text,
            start_ms: cue.start_ms, end_ms: cue.end_ms, clicked_at_ms: Math.round(video.currentTime * 1000)
          }};
          publish("probe:selection");
        });
        line.append(button);
        position = match.index + match[0].length;
      }
      line.append(document.createTextNode(cue.text.slice(position)));
      container.append(line);
    }
  }

  // Poll the video clock rather than wall time so pause, seek, and playback speed agree.
  setInterval(() => {
    const nextId = videoId();
    if (nextId !== currentId) {
      currentId = nextId;
      reset("Видео изменилось. Контекст предыдущего видео удалён.");
      if (enabled && nextId) start();
    }
    if (state.status !== "ready") return;
    const nextPlayer = document.getElementById("movie_player");
    const video = nextPlayer?.querySelector("video");
    if (!video) { detach(); return; }
    if (player !== nextPlayer || !host?.isConnected) container = attach(nextPlayer);
    // Fullscreen, Shorts, live streams and ads are outside this experiment's support.
    const hidden = Boolean(document.fullscreenElement) || nextPlayer.classList.contains("ad-showing");
    host.hidden = hidden;
    player.classList.toggle("lt-caption-probe-active", !hidden);
    if (hidden) return;
    const now = video.currentTime * 1000;
    const active = state.transcript.cues.filter(cue => cue.start_ms <= now && now < cue.end_ms).slice(-2);
    draw(active, video);
  }, 100);
})();
