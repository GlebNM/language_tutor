(() => {
  let generation = 0;
  let enabled = false;
  let currentId;
  let host, player, container;
  let lastCueKey = "";
  let state = {status:"idle",video_id:null,selection:null,message:"Включите учебный режим."};
  const videoId = () => location.pathname === "/watch" ? new URL(location.href).searchParams.get("v") : null;
  currentId = videoId(); state.video_id = currentId;
  const title = () => document.querySelector("ytd-watch-metadata h1")?.textContent?.trim() || document.title.replace(/ - YouTube$/, "");
  state.title = title();
  const publish = (type="lt:state") => chrome.runtime.sendMessage({type,state}).catch(() => {});
  function detach() {
    host?.remove(); player?.classList.remove("lt-study-active");
    host = player = container = undefined; lastCueKey = "";
  }
  function reset(message) {
    generation++; detach();
    state = {status:"idle",enabled,video_id:currentId,title:title(),selection:null,message};
    publish();
  }
  async function start(forceExtract=false) {
    currentId = videoId();
    enabled = true;
    reset("Открываем материал и получаем английскую дорожку…");
    if (!/^[A-Za-z0-9_-]{11}$/.test(currentId ?? "")) {
      enabled = false;
      state = {...state,status:"error",enabled:false,message:"Откройте обычную страницу youtube.com/watch."};publish();return;
    }
    const request = generation;
    state = {...state,status:"loading"};publish();
    try {
      const result = await chrome.runtime.sendMessage({type:"lt:load",video_id:currentId,title:title(),force_extract:forceExtract});
      if (request !== generation || videoId() !== currentId) return;
      state = {...state,status:result?.ok ? "ready" : "error",material:result?.material,evidence:result?.evidence,
        message:result?.message ?? "Расширение не ответило. Обновите страницу YouTube."};
    } catch {
      if (request !== generation) return;
      state = {...state,status:"error",message:"Расширение перезагружено. Обновите страницу YouTube."};
    }
    publish();
  }
  chrome.runtime.onMessage.addListener((message,sender,respond) => {
    if (sender.id !== chrome.runtime.id) return;
    if (message.type === "lt:get") {respond(state);return;}
    if (message.type === "lt:start") {start(Boolean(message.force_extract));respond({ok:true});return;}
    if (message.type === "lt:stop") {enabled=false;reset("Учебный режим выключен. Штатные субтитры восстановлены.");respond({ok:true});return;}
    if (message.type === "lt:material") {
      if (message.material?.youtube_video_id !== videoId()) {respond({ok:false});return;}
      generation++; enabled=true;lastCueKey="";
      const selected = state.selection;
      const cue = message.material.cues.find(item => item.id === selected?.cue.id);
      const token = cue?.tokens.find(item => item.id === selected?.token.id);
      state = {...state,status:"ready",enabled:true,video_id:videoId(),title:title(),material:message.material,
        selection:cue && token ? {...selected,cue,token} : null,message:"Материал общего профиля обновлён."};
      publish();respond({ok:true});return;
    }
    if (message.type === "lt:close") {state={...state,selection:null};publish();respond({ok:true});return;}
    if (["lt:seek","lt:resume"].includes(message.type)) {
      if (message.video_id !== videoId()) {respond({ok:false});return;}
      const video = document.querySelector("#movie_player video");
      if (video) {
        if (message.type === "lt:seek" && Number.isFinite(message.start_ms) && message.start_ms >= 0) {
          video.currentTime=message.start_ms/1000;video.pause();
        } else if (message.type === "lt:resume") video.play().catch(() => {});
      }
      respond({ok:Boolean(video)});
    }
  });
  function attach(nextPlayer) {
    detach();player=nextPlayer;player.classList.add("lt-study-active");
    host=document.createElement("div");host.setAttribute("aria-label","Английские субтитры Language Tutor");
    host.style.cssText="position:absolute;bottom:16%;left:7%;width:86%;z-index:50;pointer-events:none;text-align:center";
    const shadow=host.attachShadow({mode:"closed"});
    const style=document.createElement("style");
    style.textContent=`:host{color:white;font:clamp(17px,2.1vw,28px)/1.5 Arial,sans-serif}p{margin:3px auto;padding:3px 9px;border-radius:5px;width:fit-content;max-width:100%;background:rgba(0,0,0,.88)}button{all:unset;display:inline;color:inherit;font:inherit;cursor:pointer;pointer-events:auto;text-underline-offset:4px}button[data-status=unassessed]{text-decoration:underline dotted #9ce6ff}button[data-status=learning]{background:#56411c;text-decoration:underline #ffd577}button[data-status=unknown]{background:#713b45;text-decoration:underline #ffc2c2}button:hover,button:focus-visible{color:#ffe08a;outline:2px solid #ffe08a;outline-offset:2px}`;
    container=document.createElement("div");shadow.append(style,container);player.append(host);
  }
  function draw(cues,video) {
    const key=cues.map(cue=>cue.id).join("|");if(key===lastCueKey)return;lastCueKey=key;container.replaceChildren();
    for(const cue of cues) {
      const line=document.createElement("p");let position=0;const chars=Array.from(cue.text);
      for(const token of cue.tokens) {
        line.append(document.createTextNode(chars.slice(position,token.start_offset).join("")));
        const button=document.createElement("button");button.type="button";button.textContent=token.surface;
        const k=token.knowledge;button.dataset.status=k?.ignored||k?.score>=2?"known":k?.score===1?"learning":k?.score===0?"unknown":"unassessed";
        button.setAttribute("aria-label",`Разобрать ${token.surface}, ${Math.floor(cue.start_ms/1000)} секунда`);
        button.addEventListener("click",event=>{
          event.preventDefault();event.stopPropagation();if(!enabled||state.video_id!==videoId())return;video.pause();
          state={...state,selection:{material_id:state.material.id,cue,token,clicked_at_ms:Math.round(video.currentTime*1000)}};publish("lt:selection");
        });line.append(button);position=token.end_offset;
      }
      line.append(document.createTextNode(chars.slice(position).join("")));container.append(line);
    }
  }
  setInterval(()=>{
    const nextId=videoId();
    if(nextId!==currentId){currentId=nextId;reset("Видео изменилось. Предыдущий контекст удалён.");if(enabled&&nextId)start();}
    if(!enabled||state.status!=="ready")return;
    const nextPlayer=document.getElementById("movie_player"),video=nextPlayer?.querySelector("video");
    if(!video){detach();return;}if(player!==nextPlayer||!host?.isConnected)attach(nextPlayer);
    const hidden=Boolean(document.fullscreenElement)||nextPlayer.classList.contains("ad-showing");host.hidden=hidden;player.classList.toggle("lt-study-active",!hidden);if(hidden)return;
    const now=video.currentTime*1000;draw(state.material.cues.filter(cue=>cue.start_ms<=now&&now<cue.end_ms).slice(-2),video);
  },250);
})();
