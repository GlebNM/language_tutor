(() => {
  let generation = 0;
  let enabled = false;
  let currentId;
  let host, player, container;
  let lastCueKey = "";
  let state = {status:"idle",video_id:null,selection:null,backend_origin:null,message:"Включите учебный режим."};
  const videoId = () => location.pathname === "/watch" ? new URL(location.href).searchParams.get("v") : null;
  currentId = videoId(); state.video_id = currentId;
  const title = () => document.querySelector("ytd-watch-metadata h1")?.textContent?.trim() || document.title.replace(/ - YouTube$/, "");
  state.title = title();
  const publish = (type="lt:state") => chrome.runtime.sendMessage({type,state}).catch(() => {});
  function detach() {
    host?.remove(); player?.classList.remove("lt-study-active");
    host = player = container = undefined; lastCueKey = "";
  }
  function reset(message, backendOrigin=null) {
    generation++; detach();
    state = {status:"idle",enabled,video_id:currentId,title:title(),selection:null,backend_origin:backendOrigin,message};
    publish();
  }
  async function start(forceExtract=false, backendOrigin=null) {
    currentId = videoId();
    enabled = true;
    reset("Открываем материал и получаем английскую дорожку…",backendOrigin);
    if (!/^[A-Za-z0-9_-]{11}$/.test(currentId ?? "")) {
      enabled = false;
      state = {...state,status:"error",enabled:false,message:"Откройте обычную страницу youtube.com/watch."};publish();return;
    }
    const request = generation;
    state = {...state,status:"loading",backend_origin:backendOrigin};publish();
    try {
      const result = await chrome.runtime.sendMessage({type:"lt:load",video_id:currentId,title:title(),force_extract:forceExtract,backend_origin:backendOrigin});
      if (request !== generation || videoId() !== currentId) return;
      if (backendOrigin && result?.backend_origin !== backendOrigin) {
        state = {...state,status:"error",enabled:false,message:"Адрес общего профиля изменился. Повторите подключение."};enabled=false;publish();return;
      }
      state = {...state,status:result?.ok ? "ready" : "error",material:result?.material,evidence:result?.evidence,
        extraction_code:result?.extraction_code,backend_origin:result?.backend_origin ?? null,
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
    if (message.type === "lt:start") {start(Boolean(message.force_extract),message.backend_origin??null);respond({ok:true});return;}
    if (message.type === "lt:profile-changed") {
      enabled=false;currentId=videoId();reset("Адрес общего профиля изменён. Включите учебный режим, чтобы загрузить материал из нового профиля.",null);
      respond({ok:true});return;
    }
    if (message.type === "lt:stop") {enabled=false;reset("Учебный режим выключен. Штатные субтитры восстановлены.");respond({ok:true});return;}
    if (message.type === "lt:material") {
      const request=generation,material=message.material,origin=message.backend_origin;
      const applyMaterial=boundOrigin=>{
        generation++;enabled=true;lastCueKey="";
        const selected=state.selection;
        const cue=material.cues.find(item=>item.id===selected?.cue.id);
        const token=cue?.tokens.find(item=>item.id===selected?.token.id);
        state={...state,status:"ready",enabled:true,video_id:videoId(),title:title(),material,
          selection:cue&&token?{...selected,cue,token}:null,evidence:null,extraction_code:null,
          backend_origin:boundOrigin,message:"Материал общего профиля обновлён."};
        publish();
      };
      if(!chrome.storage?.local?.get) {
        const boundOrigin=origin??state.backend_origin;
        if(material?.youtube_video_id!==videoId()||!Array.isArray(material?.cues)||boundOrigin!==state.backend_origin){respond({ok:false});return;}
        applyMaterial(boundOrigin);respond({ok:true});return;
      }
      chrome.storage.local.get({backend:"http://127.0.0.1:8765"}).then(settings=>{
        let configuredOrigin=null;try{configuredOrigin=new URL(settings.backend).origin;}catch{}
        if(request!==generation||material?.youtube_video_id!==videoId()||!Array.isArray(material?.cues)||!origin||origin!==configuredOrigin){respond({ok:false});return;}
        applyMaterial(origin);respond({ok:true});
      }).catch(()=>respond({ok:false}));return true;
    }
    if (message.type === "lt:close") {state={...state,selection:null};publish();respond({ok:true});return;}
    if (message.type === "lt:select-occurrence") {
      if (message.video_id !== videoId() || message.material_id !== state.material?.id) {respond({ok:false});return;}
      const cue=state.material.cues.find(item=>item.id===message.cue_id);
      const token=cue?.tokens.find(item=>item.id===message.token_id);
      const video=document.querySelector("#movie_player video");
      if (!cue || !token || !video) {respond({ok:false});return;}
      video.currentTime=cue.start_ms/1000;video.pause();
      state={...state,selection:{material_id:state.material.id,cue,token,clicked_at_ms:cue.start_ms}};publish("lt:selection");respond({ok:true});return;
    }
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
  chrome.storage?.onChanged?.addListener((changes,area)=>{
    if(area!=="local"||!changes.backend)return;
    let nextOrigin=null;try{nextOrigin=new URL(changes.backend.newValue).origin;}catch{}
    if(nextOrigin!==state.backend_origin){
      enabled=false;currentId=videoId();
      reset("Адрес общего профиля изменён. Включите учебный режим, чтобы загрузить материал из нового профиля.",null);
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
    if(nextId!==currentId){const backendOrigin=state.backend_origin;currentId=nextId;reset("Видео изменилось. Предыдущий контекст удалён.",backendOrigin);if(enabled&&nextId)start(false,backendOrigin);}
    if(!enabled||state.status!=="ready")return;
    const nextPlayer=document.getElementById("movie_player"),video=nextPlayer?.querySelector("video");
    if(!video){detach();return;}if(player!==nextPlayer||!host?.isConnected)attach(nextPlayer);
    const hidden=Boolean(document.fullscreenElement)||nextPlayer.classList.contains("ad-showing");host.hidden=hidden;player.classList.toggle("lt-study-active",!hidden);if(hidden)return;
    const now=video.currentTime*1000;draw(state.material.cues.filter(cue=>cue.start_ms<=now&&now<cue.end_ms).slice(-2),video);
  },250);
})();
