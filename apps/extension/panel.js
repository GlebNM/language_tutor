import {api, backendUrl, DEFAULT_BACKEND, knowledgeLabel, orderVideoVocabulary, permissionOrigin, scores, timestamp, videoVocabulary} from "./shared.js";

const app=document.getElementById("app");
let tabId=null,state={},backend=DEFAULT_BACKEND,view="material",error="",notice="";
let backendResetPending=false;
let generation=0,selectionKey="",vocabularyContext="",card=null,cardError="",cardBusy=false,cardAbort;
let chat=null,chatOpen=false,chatBusy=false,chatError="",question="",retry=null;
let dictionary=[],dictionaryLoaded=false,filters={search:"",score:"",ignored:"false"};
let videoFilters={search:"",score:"",ignored:""},videoExpandedId=null;
let aiSettings=null,aiModels=[],aiSettingsLoading=false,aiSettingsError="",aiDraft=null;
let aiSettingsRequest=0;
let diagnostic=null,diagnosticLoaded=false,diagnosticAnswer="",busy=false;
const drafts=new Map();
const node=(tag,attributes={},...children)=>{
  const element=document.createElement(tag);
  for(const [key,value] of Object.entries(attributes)) {
    if(key.startsWith("on"))element.addEventListener(key.slice(2).toLowerCase(),value);
    else if(key==="class")element.className=value;
    else if(key==="text")element.textContent=value;
    else if(key in element && !key.startsWith("aria"))element[key]=value;
    else element.setAttribute(key,String(value));
  }
  for(const child of children.flat())if(child!=null)element.append(typeof child==="string"?document.createTextNode(child):child);
  return element;
};
const button=(text,onClick,secondary=false)=>node("button",{type:"button",class:secondary?"secondary":"",onClick},text);
const paragraph=(text,className="")=>node("p",{class:className},text);
const selection=()=>state.selection;
const safeUrl=value=>{try{const url=new URL(value);return url.protocol==="https:"&&["www.youtube.com","youtube.com","youtu.be"].includes(url.hostname)?url.href:null;}catch{return null;}};
async function message(type,extra={}) {
  if(tabId==null)throw new Error("Откройте видео на YouTube.");
  try{return await chrome.tabs.sendMessage(tabId,{type,...extra});}
  catch{throw new Error("Панель не связалась с YouTube. Обновите страницу видео после установки расширения.");}
}
async function action(work,{success="",refresh=false}={}) {
  const version=generation;error="";notice="";busy=true;render();
  try{await work();if(version!==generation)return;notice=success;if(refresh)await refreshMaterial();}
  catch(e){if(version===generation)error=e.message;}
  finally{if(version===generation){busy=false;render();}}
}
function acceptState(next) {
  const context=`${next?.video_id??""}:${next?.material?.id??""}`;
  if(context!==vocabularyContext){vocabularyContext=context;videoExpandedId=null;videoFilters={search:"",score:"",ignored:""};}
  state=next??{};
  const selected=selection();
  const key=`${tabId}:${state.video_id??""}:${selected?.material_id??""}:${selected?.token.id??""}:${selected?.token.lexeme_id??""}`;
  if(key!==selectionKey){
    generation++;selectionKey=key;cardAbort?.abort();card=null;cardError="";cardBusy=false;
    chat=null;chatOpen=false;chatBusy=false;chatError="";question="";retry=null;error="";notice="";busy=false;
    if(selected)void loadCard();
  }
  render();
}
async function syncTab() {
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  const target=tab?.id??null;tabId=target;
  try{const next=await message("lt:get");if(target===tabId&&!backendResetPending)acceptState(next);}
  catch{if(target===tabId)acceptState({message:"Откройте обычное видео YouTube. Если оно уже открыто, обновите страницу после установки расширения."});}
}
async function refreshMaterial() {
  if(!state.material)return;
  const version=generation,material=await api(`/materials/${encodeURIComponent(state.material.id)}`);
  if(version!==generation)return;
  await message("lt:material",{material});
  if(version===generation)acceptState(await message("lt:get"));
}
async function loadCard() {
  const selected=selection();if(!selected)return;
  const version=generation;cardAbort?.abort();cardAbort=new AbortController();
  cardBusy=true;cardError="";render();
  try{
    const result=await api("/cards",{method:"POST",signal:cardAbort.signal,body:JSON.stringify({material_id:selected.material_id,cue_id:selected.cue.id,token_id:selected.token.id,force_refresh:false})});
    if(version===generation)card=result;
  }catch(e){if(version===generation&&e.name!=="AbortError")cardError=e.message;}
  finally{if(version===generation){cardBusy=false;render();}}
}
function aiMeta(value) {
  if(!value)return "";
  const model=value.model??value.response?.model;
  if(!model)return "";
  const hasEffort=value.reasoning_effort!==undefined||Object.hasOwn(value.response??{},"reasoning_effort");
  const effort=value.reasoning_effort!==undefined?value.reasoning_effort:value.response?.reasoning_effort;
  return `Модель: ${model} · reasoning: ${hasEffort?(effort===null?"по умолчанию":effort):"не записан"}`;
}
function familiarity(token,{numericLabels=false}={}) {
  const id=token.lexeme_id??token.id;
  const saved={score:String(token.knowledge?.score??""),ignored:token.knowledge?.ignored??false,dirty:false,error:""};
  const draft=drafts.get(id)??saved;drafts.set(id,draft);
  if(!draft.dirty){draft.score=saved.score;draft.ignored=saved.ignored;}
  const label=node("p",{class: draft.dirty?"warning":"help"},draft.dirty?"Изменения не сохранены":knowledgeLabel(token.knowledge));
  const save=node("button",{type:"submit",disabled:busy||!draft.dirty},draft.error?"Повторить сохранение":"Сохранить знакомость");
  const dirty=()=>{draft.dirty=true;label.textContent="Изменения не сохранены";label.className="warning";save.disabled=false;};
  const score=node("select",{value:draft.score,onChange:event=>{draft.score=event.target.value;dirty();}},
    node("option",{value:""},"Пока не оценено"),scores.map((text,value)=>node("option",{value:String(value)},numericLabels?`${value} · ${text}`:text)));
  score.value=draft.score;
  const form=node("form",{class:"knowledge",onSubmit:event=>{
    event.preventDefault();void action(async()=>{
      try{await api(`/lexemes/${encodeURIComponent(id)}/knowledge`,{method:"PUT",body:JSON.stringify({score:draft.score===""?null:Number(draft.score),ignored:draft.ignored})});draft.dirty=false;draft.error="";
        if(view==="dictionary")await loadDictionary(false);await refreshMaterial();}
      catch(e){draft.error=e.message;throw e;}
    },{success:"Знакомость сохранена в общем профиле."});
  }},node("label",{},"Знакомость",score),node("label",{class:"checkbox"},node("input",{type:"checkbox",checked:draft.ignored,onChange:event=>{draft.ignored=event.target.checked;dirty();}}),"Исключить из оценки"),label);
  if(token.knowledge?.provisional)form.append(paragraph(`Предварительная оценка · ${token.knowledge.source==="prior"?"предположение по частотной группе":"ответ в диагностике"}`,"help"));
  if(draft.error)form.append(paragraph(draft.error,"warning"));form.append(save);return form;
}
function difficultyPanel() {
  const value=state.material?.difficulty;if(!value)return null;
  const familiar=value.known_percent??value.familiar_percent;
  const section=node("section",{},node("h2",{},"Лексика материала"));
  if(value.provisional)section.append(node("span",{class:"badge"},"Предварительно"));
  if(typeof familiar==="number")section.append(node("div",{class:"bar","aria-hidden":true},node("span",{style:`width:${familiar}%`}),node("i",{style:`width:${value.unfamiliar_percent??0}%`})),
    paragraph(`${familiar.toFixed(1)}% знакомо · ${Number(value.unfamiliar_percent??0).toFixed(1)}% незнакомо · ${Number(value.unassessed_percent??0).toFixed(1)}% пока не оценено`));
  if(value.label)section.append(node("strong",{},value.label));
  if(value.reason)section.append(paragraph(value.reason));
  if(value.unique_unfamiliar!=null)section.append(paragraph(`Уникальных незнакомых слов: ${value.unique_unfamiliar}`,"help"));
  if(value.provisional_percent!=null)section.append(paragraph(`Предварительные оценки: ${value.provisional_percent}%`,"help"));
  for(const limitation of Array.isArray(value.limitations)?value.limitations:[])section.append(paragraph(limitation,"help"));
  section.append(paragraph("Показатель описывает лексику. Скорость речи, акцент и грамматика влияют на понимание отдельно.","help"));return section;
}
function extractionDetails() {
  const evidence=state.evidence;if(!evidence)return null;
  const source={"youtube-authored":"Авторская английская дорожка","youtube-automatic":"Автоматическая исходная английская дорожка"}[evidence.source]??evidence.source;
  const checked=Date.parse(evidence.checked_at);
  const rows=[
    state.extraction_code?`Код: ${state.extraction_code}`:null,
    Number.isFinite(checked)?`Проверено: ${new Date(checked).toLocaleString()}`:null,
    evidence.video_id?`Видео: ${evidence.video_id}`:null,
    source?`Источник: ${source}`:null,
    evidence.track_language?`Язык дорожки: ${evidence.track_language}`:null,
    Number.isInteger(evidence.http_status)?`HTTP: ${evidence.http_status}`:null,
    evidence.content_type?`Тип ответа: ${evidence.content_type}`:null,
    Number.isInteger(evidence.content_length)?`Размер по заголовку: ${evidence.content_length} байт`:null,
    Number.isInteger(evidence.response_bytes)?`Получено: ${evidence.response_bytes} байт`:null,
    evidence.transcript_hash?`Хеш дорожки: ${evidence.transcript_hash}`:null,
  ].filter(Boolean);
  if(!rows.length)return null;
  return node("details",{},node("summary",{},state.extraction_code?"Подробности проверки":"Данные дорожки"),rows.map(value=>paragraph(value,"help")));
}
function videoVocabularyPanel() {
  const material=state.material;
  const coverage=material?.is_complete
    ? "Все базовые формы полной дорожки. Выберите слово, чтобы увидеть формы, оценку и вхождения."
    : "Список охватывает только загруженную часть субтитров; для оценки всего видео он неполный. Выберите слово, чтобы увидеть формы, оценку и вхождения.";
  const panel=node("section",{},node("h2",{},"Слова видео"),paragraph(coverage,"help"));
  if(!material?.cues){panel.append(paragraph("Сначала включите учебный режим и загрузите дорожку.","empty"));return panel;}
  const grouped=orderVideoVocabulary(videoVocabulary(material.cues));
  const query=videoFilters.search.trim().toLocaleLowerCase();
  const filtered=grouped.filter(entry=>{
    if(query&&!`${entry.lemma} ${entry.part_of_speech} ${entry.forms.join(" ")}`.toLocaleLowerCase().includes(query))return false;
    const score=entry.knowledge?.score;
    if(videoFilters.score==="unassessed"&&score!=null)return false;
    if(videoFilters.score!==""&&videoFilters.score!=="unassessed"&&String(score)!==videoFilters.score)return false;
    if(videoFilters.ignored!==""&&String(Boolean(entry.knowledge?.ignored))!==videoFilters.ignored)return false;
    return true;
  });
  const search=node("input",{value:videoFilters.search,placeholder:"Базовая форма или словоформа"});
  const score=node("select",{},node("option",{value:""},"Все уровни"),node("option",{value:"unassessed"},"Пока не оценено"),scores.map((label,index)=>node("option",{value:String(index)},`${index} · ${label}`)));score.value=videoFilters.score;
  const ignored=node("select",{},node("option",{value:""},"Все слова"),node("option",{value:"false"},"Учитываемые"),node("option",{value:"true"},"Исключённые"));ignored.value=videoFilters.ignored;
  panel.append(node("form",{onSubmit:event=>{event.preventDefault();videoFilters={search:search.value,score:score.value,ignored:ignored.value};videoExpandedId=null;render();}},node("label",{},"Поиск",search),node("label",{},"Знакомость",score),node("label",{},"Исключённые",ignored),node("button",{},"Применить фильтры")));
  const labelFor=entry=>entry.knowledge?.score==null?"Пока не оценено":`${entry.knowledge.score} · ${scores[entry.knowledge.score]}`;
  const legend=node("div",{class:"vocab-legend","aria-label":"Цвета знакомости"});
  for(const [key,label]of[["unassessed","Пока не оценено"],...[0,1,2,3].map(level=>[`score-${level}`,`${level} · ${scores[level]}`])])legend.append(node("span",{class:"legend-item"},node("i",{class:`vocab-swatch ${key}`,"aria-hidden":"true"}),label));
  panel.append(legend,paragraph(`${filtered.length} базовых форм · нажмите слово, чтобы раскрыть его детали`,"help"));
  const sections=[[null,"Пока не оценено"],...[0,1,2,3].map(level=>[level,`${level} · ${scores[level]}`])];
  for(const [level,title]of sections){
    const entries=filtered.filter(entry=>(entry.knowledge?.score??null)===level);
    if(!entries.length)continue;
    const group=node("section",{class:"vocab-group"},node("h3",{},`${title} · ${entries.length}`),node("div",{class:"vocab-grid"}));
    const grid=group.lastElementChild;
    for(const entry of entries){
      const active=videoExpandedId===entry.id;
      const key=level==null?"unassessed":`score-${level}`;
      const chip=node("button",{type:"button",class:`vocab-chip ${key}${active?" active":""}${entry.knowledge?.ignored?" excluded":""}`,"aria-expanded":String(active),onClick:()=>{videoExpandedId=active?null:entry.id;render();}},
        node("strong",{},entry.lemma),node("span",{class:"chip-pos"},entry.part_of_speech),node("span",{class:"chip-count"},`${entry.count}×`),node("span",{class:"chip-score"},labelFor(entry)));
      if(entry.knowledge?.ignored)chip.append(node("span",{class:"chip-ignored"},"Исключено"));
      if(entry.knowledge?.provisional)chip.append(node("span",{class:"chip-provisional"},"Предварительно"));
      grid.append(chip);
      if(active){
        const details=node("div",{class:"vocab-detail"},node("p",{},`Словоформы: ${entry.forms.join(", ")}`),paragraph(`Знакомость: ${labelFor(entry)}${entry.knowledge?.ignored?" · исключено из оценки":""}`,"help"));
        if(!entry.lexeme_id)details.append(paragraph("Для этой формы сервер не вернул ID словаря.","warning"));
        else details.append(familiarity(entry,{numericLabels:true}));
        if(entry.knowledge?.provisional)details.append(node("span",{class:"badge"},"Предварительная оценка"));
        const occurrences=node("div",{class:"vocab-occurrences"});
        for(const occurrence of entry.occurrences)occurrences.append(node("button",{type:"button",class:"secondary timestamp",onClick:()=>void action(async()=>{
          const result=await message("lt:select-occurrence",{video_id:state.video_id,material_id:material.id,cue_id:occurrence.cue_id,token_id:occurrence.token_id});
          if(!result?.ok)throw new Error("Не удалось открыть выбранное вхождение. Обновите дорожку и повторите попытку.");
        })},`${timestamp(occurrence.start_ms)} · ${occurrence.text}`));
        details.append(node("h4",{},`Вхождения · ${entry.count}`),occurrences);grid.append(details);
      }
    }
    panel.append(group);
  }
  return panel;
}
function importForm() {
  let file=null;
  const status=paragraph("Файл до 2 МБ, до 10 000 реплик и 200 000 символов текста.","help");
  const complete=node("input",{type:"checkbox",checked:true});
  return node("details",{open:state.status==="error"||!state.material},node("summary",{},state.material?"Заменить дорожку SRT/VTT":"Импортировать SRT/VTT"),
    node("form",{onSubmit:event=>{
      event.preventDefault();if(!file){status.textContent="Выберите SRT или VTT.";return;}
      const version=generation,currentVideo=state.video_id,oldMaterial=state.material;
      void action(async()=>{
        if(file.size>2_000_000)throw new Error("Файл превышает 2 МБ.");
        const format=file.name.toLowerCase().endsWith(".vtt")?"vtt":file.name.toLowerCase().endsWith(".srt")?"srt":null;
        if(!format)throw new Error("Выберите файл SRT или VTT.");
        const content=await file.text();if(version!==generation)return;
        const payload={youtube_url:`https://www.youtube.com/watch?v=${currentVideo}`,title:state.title??"",format,content,language:"en",source:format,is_complete:complete.checked};
        const summary=await api(oldMaterial?`/materials/${encodeURIComponent(oldMaterial.id)}/track`:"/materials",{method:oldMaterial?"PUT":"POST",body:JSON.stringify(payload)});
        if(version!==generation)return;const material=await api(`/materials/${encodeURIComponent(summary.id)}`);if(version!==generation)return;
        await message("lt:material",{material});acceptState(await message("lt:get"));notice="Дорожка проверена и сохранена. Предыдущая версия остаётся в библиотеке.";
      });
    }},node("label",{},"Английская дорожка текущего видео",node("input",{type:"file",accept:".srt,.vtt",required:true,onChange:event=>{file=event.target.files[0];}})),
      node("label",{class:"checkbox"},complete,"Это полная дорожка видео"),status,node("button",{disabled:busy||!state.video_id},state.material?"Проверить и заменить":"Проверить и импортировать")));
}
function materialView() {
  const fragment=document.createDocumentFragment();
  const controls=node("section",{},node("h2",{},"Учебный режим"),paragraph(state.message??"Откройте обычное видео YouTube.","status"));
  const details=extractionDetails();if(details)controls.append(details);
  const enable=button(state.status==="loading"?"Загружаем…":state.enabled?"Выключить слой":"Включить учебный режим",()=>void action(async()=>{
    if(state.enabled){await message("lt:stop");return;}
    if(!await chrome.permissions.request({origins:[permissionOrigin(backend)]}))throw new Error("Для общего профиля нужен доступ к выбранному локальному серверу.");
    backendResetPending=false;
    await message("lt:start");
  }),state.enabled);enable.disabled=state.status==="loading"||busy;controls.append(enable);
  if(state.material){
    const source={"youtube-authored":"Авторские английские субтитры","youtube-automatic":"Автоматические исходные английские субтитры",srt:"Импорт SRT",vtt:"Импорт VTT",demo:"Подготовленная дорожка"}[state.material.source]??state.material.source;
    controls.append(paragraph(`${source} · ${state.material.is_complete?"полная дорожка":"неполная дорожка"} · версия ${state.material.version}`,"help"));
    controls.append(button("Обновить данные профиля",()=>void action(()=>refreshMaterial()),true));
  }
  if(state.video_id){controls.append(importForm());controls.append(button("Повторить автоматическое извлечение",()=>void action(async()=>{
    if(!await chrome.permissions.request({origins:[permissionOrigin(backend)]}))throw new Error("Разрешите доступ к локальному серверу.");await message("lt:start",{force_extract:true});
  }),true));}
  fragment.append(controls);
  const difficulty=difficultyPanel();if(difficulty)fragment.append(difficulty);
  if(state.material)fragment.append(videoVocabularyPanel());
  if(selection())fragment.append(selectionPanel());
  else if(state.material)fragment.append(paragraph("Нажмите слово в субтитрах поверх YouTube. Видео остановится, карточка закрепит выбранную реплику.","empty"));
  return fragment;
}
function selectionPanel() {
  const selected=selection(),token=selected.token;
  const panel=node("section",{},node("h2",{},"Слово в контексте"),node("div",{class:"selected-word"},token.surface),paragraph(`${token.lemma} · ${token.part_of_speech}`,"help"),
    paragraph(selected.cue.text,"quote"),button(`${timestamp(selected.cue.start_ms)} · К реплике`,()=>void action(()=>message("lt:seek",{video_id:state.video_id,start_ms:selected.cue.start_ms})),true));
  const controls=node("div",{class:"row"},button("Продолжить просмотр",()=>void action(()=>message("lt:resume",{video_id:state.video_id}))),button("Закрыть карточку",()=>void action(()=>message("lt:close")),true));panel.append(controls);
  if(cardBusy)panel.append(paragraph("Разбираем слово в контексте…","status"));
  if(cardError)panel.append(paragraph(cardError,"warning"),button("Повторить разбор",()=>void loadCard(),true),node("a",{href:backend,target:"_blank",rel:"noreferrer"},"Настройки подписки в приложении ↗"));
  if(card?.card){const value=card.card;panel.append(node("h3",{},value.translation),paragraph(value.explanation),paragraph(value.expression,"expression"),paragraph(`Учебный пример: ${value.example}`,"example"));
    if(aiMeta(card))panel.append(paragraph(aiMeta(card),"help"));
    panel.append(node("button",{type:"button",class:"secondary",disabled:cardBusy,onClick:()=>void loadCardFresh()},cardBusy?"Генерируем…":"Сгенерировать заново"));
    if(card.tools?.length)panel.append(paragraph(`Инструменты: ${card.tools.map(tool=>typeof tool==="string"?tool:tool.name??tool.tool_name??"MCP").join(", ")}`,"help"));}
  panel.append(familiarity(token));
  const lemma=node("input",{value:token.lemma,required:true,maxLength:100}),pos=node("select",{},["NOUN","VERB","ADJ","ADV","PROPN","AUX","PRON","ADP","DET","CCONJ","SCONJ","PART","INTJ","NUM","X"].map(value=>node("option",{value},value)));pos.value=token.part_of_speech;
  panel.append(node("details",{},node("summary",{},"Исправить базовую форму"),node("form",{onSubmit:event=>{
    event.preventDefault();void action(()=>api(`/materials/${selected.material_id}/occurrences/${token.id}/lexeme`,{method:"PUT",body:JSON.stringify({cue_id:selected.cue.id,lemma:lemma.value,part_of_speech:pos.value})}),{success:"Базовая форма исправлена.",refresh:true});
  }},node("label",{},"Базовая форма",lemma),node("label",{},"Часть речи",pos),node("button",{disabled:busy},"Сохранить форму"))));
  panel.append(button(chatOpen?"Скрыть диалог":"Обсудить с агентом",()=>{
    chatOpen=!chatOpen;render();if(chatOpen&&!chat)void openChat();
  },true));if(chatOpen)panel.append(chatPanel());return panel;
}
async function loadCardFresh() {
  const selected=selection();if(!selected)return;
  const version=generation;cardAbort?.abort();cardAbort=new AbortController();cardBusy=true;cardError="";render();
  try{
    const result=await api("/cards",{method:"POST",signal:cardAbort.signal,body:JSON.stringify({material_id:selected.material_id,cue_id:selected.cue.id,token_id:selected.token.id,force_refresh:true})});
    if(version===generation)card=result;
  }catch(e){if(version===generation&&e.name!=="AbortError")cardError=e.message;}
  finally{if(version===generation){cardBusy=false;render();}}
}
async function openChat() {
  const version=generation,selected=selection();if(!selected)return;chatBusy=true;chatError="";render();
  try{const value=await selectedConversation(selected);if(version===generation)chat=value;}
  catch(e){if(version===generation)chatError=e.message;}
  finally{if(version===generation){chatBusy=false;render();}}
}
async function selectedConversation(selected) {
  const list=await api(`/conversations?material_id=${encodeURIComponent(selected.material_id)}`);
  const saved=list.find(item=>item.cue_id===selected.cue.id&&item.token_id===selected.token.id);
  return saved?api(`/conversations/${encodeURIComponent(saved.id)}`):api("/conversations",{method:"POST",body:JSON.stringify({material_id:selected.material_id,cue_id:selected.cue.id,token_id:selected.token.id})});
}
async function sendChat() {
  const text=question.trim();if(!text||chatBusy)return;
  const version=generation,selected=selection(),payload=retry?.message===text?retry:{message:text,request_id:crypto.randomUUID()};retry=payload;chatBusy=true;chatError="";render();
  try{
    const current=chat??await selectedConversation(selected);
    if(version!==generation)return;chat=current;
    await api(`/conversations/${encodeURIComponent(current.id)}/messages`,{method:"POST",body:JSON.stringify(payload)});
    if(version!==generation)return;
    const value=await api(`/conversations/${encodeURIComponent(current.id)}`);
    if(version===generation){chat=value;question="";retry=null;}
  }catch(e){if(version===generation)chatError=e.message;}
  finally{if(version===generation){chatBusy=false;render();}}
}
function chatPanel() {
  const panel=node("div",{},node("h3",{},"Учебный агент"),paragraph("Спросите о значении или попросите найти слово в предыдущих материалах.","help"));
  const entries=(chat?.messages??[]).flatMap(turn=>[
    {role:"user",content:turn.message},
    ...(turn.response?[{role:"assistant",content:turn.response.answer,citations:turn.response.citations,tools:turn.response.tools,example:turn.response.educational_example,model:turn.response.model,reasoning_effort:turn.response.reasoning_effort}]:[]),
  ]);
  for(const entry of entries){
    const block=node("div",{class:`message ${entry.role}`},node("small",{},entry.role==="user"?"Вы":"Агент"),paragraph(entry.content));
    if(aiMeta(entry))block.append(paragraph(aiMeta(entry),"help"));
    if(entry.example)block.append(paragraph(`Учебный пример: ${entry.example}`,"example"));
    for(const cite of entry.citations??[]){
      const quote=node("blockquote",{},cite.text);
      if(cite.material_id===selection().material_id)quote.append(button(`${cite.title??"Текущий материал"} · ${timestamp(cite.start_ms)}`,()=>void action(()=>message("lt:seek",{video_id:state.video_id,start_ms:cite.start_ms})),true));
      else {const url=safeUrl(cite.youtube_url??(/^[A-Za-z0-9_-]{11}$/.test(cite.youtube_video_id??"")?`https://www.youtube.com/watch?v=${cite.youtube_video_id}&t=${Math.floor(cite.start_ms/1000)}`:null));if(url)quote.append(node("a",{href:url,target:"_blank",rel:"noreferrer",class:"timestamp"},`${cite.title??"Предыдущий материал"} · ${timestamp(cite.start_ms)} ↗`));}
      block.append(quote);
    }
    if(entry.tools?.length)block.append(paragraph(`MCP: ${entry.tools.map(tool=>typeof tool==="string"?tool:tool.name??tool.tool_name??"вызов").join(", ")}`,"help"));panel.append(block);
  }
  const submit=node("button",{disabled:chatBusy||!question.trim()},chatBusy?"Агент отвечает…":chatError?"Повторить вопрос":"Отправить");
  const input=node("textarea",{value:question,required:true,maxLength:4000,placeholder:"Покажи это слово в моих предыдущих материалах",onInput:event=>{question=event.target.value;submit.disabled=chatBusy||!question.trim();}});
  if(chatError)panel.append(paragraph(chatError,"warning"));
  panel.append(node("form",{onSubmit:event=>{event.preventDefault();void sendChat();}},node("label",{},"Ваш вопрос",input),submit));return panel;
}
async function loadDictionary(showBusy=true) {
  const query=new URLSearchParams();for(const [key,value]of Object.entries(filters))if(value)query.set(key,value);
  if(showBusy)busy=true;render();
  try{dictionary=await api(`/dictionary?${query}`);dictionaryLoaded=true;}
  finally{if(showBusy)busy=false;render();}
}
function dictionaryView() {
  const panel=node("section",{},node("h2",{},"Мои слова"),paragraph("Словарь общий с веб-приложением. Ручные оценки имеют приоритет над диагностикой.","help"));
  const search=node("input",{value:filters.search,placeholder:"Базовая форма"}),score=node("select",{},node("option",{value:""},"Все оценки"),node("option",{value:"unassessed"},"Пока не оценено"),scores.map((label,index)=>node("option",{value:String(index)},label))),ignored=node("select",{},node("option",{value:""},"Все слова"),node("option",{value:"false"},"Учитываемые"),node("option",{value:"true"},"Исключённые"));score.value=filters.score;ignored.value=filters.ignored;
  panel.append(node("form",{onSubmit:event=>{event.preventDefault();filters={search:search.value,score:score.value,ignored:ignored.value};void action(()=>loadDictionary());}},node("label",{},"Поиск",search),node("label",{},"Знакомость",score),node("label",{},"Исключённые",ignored),node("button",{disabled:busy},"Обновить словарь")));
  if(!dictionary.length)panel.append(paragraph(dictionaryLoaded?"Подходящих слов пока нет.":"Загружаем словарь…","empty"));
  for(const entry of dictionary)panel.append(node("article",{},node("h3",{},`${entry.lemma} · ${entry.part_of_speech}`),familiarity(entry)));return panel;
}
const sessionFrom=result=>result?.session??result;
async function loadDiagnostic() {
  diagnostic=(await api("/diagnostics")).session;diagnosticLoaded=true;render();
}
function diagnosticView() {
  const panel=node("section",{},node("h2",{},"Стартовая диагностика"),paragraph("Диагностика предлагает предварительную знакомость. Её можно пропустить и исправлять оценки во время просмотра.","help"));
  panel.append(button("Обновить результат",()=>void action(()=>loadDiagnostic()),true));
  if(!diagnosticLoaded){panel.append(paragraph("Загружаем состояние…","status"));return panel;}
  if(!diagnostic||diagnostic.status!=="in_progress"){
    if(diagnostic?.summary){panel.append(node("h3",{},"Результат"),paragraph(diagnostic.summary.description??"Диагностика завершена."));
      if(aiMeta(diagnostic.summary)||aiMeta(diagnostic))panel.append(paragraph(aiMeta(diagnostic.summary)||aiMeta(diagnostic),"help"));
      for(const grade of diagnostic.summary.grades??[])panel.append(paragraph(grade.explanation,"help"));}
    if(diagnostic?.status==="skipped")panel.append(paragraph("Вы начали без начальной оценки."));
    panel.append(button(diagnostic?"Пройти диагностику заново":"Начать диагностику",()=>void action(async()=>{diagnostic=sessionFrom(await api("/diagnostics",{method:"POST",body:"{}"}));})),
      button("Начать без оценки",()=>void action(async()=>{const value=sessionFrom(await api("/diagnostics",{method:"POST",body:"{}"}));diagnostic=sessionFrom(await api(`/diagnostics/${value.id}/skip`,{method:"POST",body:"{}"}));await refreshMaterial();}),true));return panel;
  }
  const answered=new Set((diagnostic.answers??[]).map(answer=>answer.item_id)),item=diagnostic.items.find(value=>!answered.has(value.id));
  panel.append(paragraph(`Пройдено ${answered.size} из ${diagnostic.items.length}`,"help"));
  const answer=async(actionName)=>{
    diagnostic=sessionFrom(await api(`/diagnostics/${diagnostic.id}/answers`,{method:"POST",body:JSON.stringify({item_id:item.id,answer:actionName==="answer"?diagnosticAnswer:"",action:actionName})}));diagnosticAnswer="";
  };
  if(item){panel.append(node("h3",{},item.kind==="sentence"?"Объясните реплику":"Как вы понимаете слово?"),paragraph(item.prompt,"quote"),node("form",{onSubmit:event=>{event.preventDefault();void action(()=>answer("answer"));}},
    node("label",{},"Ваш ответ",node("textarea",{value:diagnosticAnswer,required:true,maxLength:2000,onInput:event=>{diagnosticAnswer=event.target.value;}})),node("button",{disabled:busy},"Сохранить ответ")),
    node("div",{class:"row"},button("Не знаю",()=>void action(()=>answer("dont_know")),true),button("Пропустить вопрос",()=>void action(()=>answer("skip")),true)));
  }else panel.append(button("Получить оценку с агентом",()=>void action(async()=>{diagnostic=sessionFrom(await api(`/diagnostics/${diagnostic.id}/complete`,{method:"POST",body:"{}"}));await refreshMaterial();},{success:"Диагностика сохранена. Знакомость и сложность обновлены."})));
  panel.append(button("Пропустить диагностику",()=>void action(async()=>{diagnostic=sessionFrom(await api(`/diagnostics/${diagnostic.id}/skip`,{method:"POST",body:"{}"}));await refreshMaterial();}),true));return panel;
}
function settingsView() {
  const input=node("input",{type:"url",required:true,value:backend});
  const settings=node("section",{},node("h2",{},"Локальный сервер"),paragraph("Панель использует тот же единственный учебный профиль, что и приложение. Подписка подключается в локальном приложении.","help"),
    node("form",{onSubmit:event=>{event.preventDefault();void action(async()=>{
      const next=backendUrl(input.value);
      if(!await chrome.permissions.request({origins:[permissionOrigin(next)]}))throw new Error("Доступ к локальному серверу не разрешён.");
      await chrome.storage.local.set({backend:next});await backendChanged(next);await api("/health");
    },{success:"Локальный сервер подключён."});}},node("label",{},"Адрес backend",input),node("button",{disabled:busy},"Сохранить и проверить")),
    paragraph("Разрешите точный origin расширения в ALLOWED_ORIGINS и пересоздайте app, как описано в инструкции установки.","help"),node("code",{},chrome.runtime.getURL("").replace(/\/$/,"")),
    node("div",{class:"links"},node("a",{href:backend,target:"_blank",rel:"noreferrer"},"Открыть приложение и подписку ↗")));
  settings.append(aiSettingsPanel());return settings;
}
async function loadAISettings() {
  const request=++aiSettingsRequest,requestedBackend=backend;
  aiSettingsLoading=true;aiSettingsError="";render();
  try {
    const [settings,catalog]=await Promise.all([api("/ai/settings"),api("/ai/models")]);
    if(request!==aiSettingsRequest||requestedBackend!==backend)return;
    aiSettings=settings;aiModels=catalog.models??[];aiDraft={model:settings.model,reasoning_effort:settings.reasoning_effort};
  }catch(error){if(request===aiSettingsRequest&&requestedBackend===backend)aiSettingsError=error.message;}
  finally{if(request===aiSettingsRequest&&requestedBackend===backend){aiSettingsLoading=false;render();}}
}
function aiSettingsPanel() {
  const panel=node("section",{},node("h2",{},"Модель и reasoning"),paragraph("Общий выбор применяется к карточкам, диагностике и диалогу. Сохранённые ответы сохраняют свои параметры.","help"));
  panel.append(button("Обновить каталог и настройки",()=>void loadAISettings(),true));
  if(aiSettingsLoading){panel.append(paragraph("Загружаем доступные модели…","status"));return panel;}
  if(aiSettingsError){panel.append(paragraph(aiSettingsError,"warning"),button("Повторить загрузку",()=>void loadAISettings(),true));return panel;}
  if(!aiDraft){panel.append(button("Загрузить настройки AI",()=>void loadAISettings(),true));return panel;}
  if(!aiModels.length){panel.append(paragraph("Backend не вернул доступных моделей.","warning"));return panel;}
  const selected=aiModels.find(value=>value.id===aiDraft.model);
  if(!selected){
    const unavailable=node("option",{value:aiDraft.model,disabled:true},`${aiSettings?.model??aiDraft.model} · недоступна в каталоге`);
    const model=node("select",{},unavailable,...aiModels.map(value=>node("option",{value:value.id},value.name??value.id)));model.value=aiDraft.model;
    panel.append(paragraph("Сохранённая модель отсутствует в текущем каталоге. Выберите модель из списка и сохраните её; запросы со старой моделью завершатся обычной ошибкой.","warning"));
    const effort=node("select",{},node("option",{value:aiDraft.reasoning_effort??"",disabled:true},aiDraft.reasoning_effort??"По умолчанию провайдера"));effort.value=aiDraft.reasoning_effort??"";
    model.addEventListener("change",()=>{const next=aiModels.find(value=>value.id===model.value);aiDraft={model:next.id,reasoning_effort:next.default_reasoning_effort??(next.reasoning_efforts?.[0]??null)};render();});
    panel.append(node("form",{onSubmit:event=>{event.preventDefault();void action(async()=>{
      aiSettings=await api("/ai/settings",{method:"PUT",body:JSON.stringify(aiDraft)});aiDraft={model:aiSettings.model,reasoning_effort:aiSettings.reasoning_effort};
    },{success:"Модель и reasoning сохранены для обоих интерфейсов."});}},node("label",{},"Модель",model),node("label",{},"Reasoning",effort),node("button",{type:"submit",disabled:busy},"Сохранить выбор")));
    return panel;
  }
  const model=node("select",{},aiModels.map(value=>node("option",{value:value.id},value.name??value.id)));model.value=selected.id;
  const efforts=selected.reasoning_efforts??[];
  const effort=node("select",{},efforts.map(value=>node("option",{value},value)));
  if(selected.default_reasoning_effort==null)effort.prepend(node("option",{value:""},"По умолчанию провайдера"));
  const currentEffort=aiDraft.reasoning_effort??"";
  const supportedEffort=currentEffort===""?selected.default_reasoning_effort==null:efforts.includes(currentEffort);
  if(!supportedEffort){
    effort.prepend(node("option",{value:currentEffort,disabled:true},`${currentEffort||"По умолчанию провайдера"} · недоступен`));
  }
  effort.value=currentEffort;
  model.addEventListener("change",()=>{const next=aiModels.find(value=>value.id===model.value);aiDraft={model:next.id,reasoning_effort:next.default_reasoning_effort??(next.reasoning_efforts?.[0]??null)};render();});
  effort.addEventListener("change",()=>{aiDraft.reasoning_effort=effort.value===""?null:effort.value;});
  panel.append(node("form",{onSubmit:event=>{event.preventDefault();void action(async()=>{
    aiSettings=await api("/ai/settings",{method:"PUT",body:JSON.stringify(aiDraft)});aiDraft={model:aiSettings.model,reasoning_effort:aiSettings.reasoning_effort};
  },{success:"Модель и reasoning сохранены для обоих интерфейсов."});}},node("label",{},"Модель",model),node("label",{},"Reasoning",effort),node("button",{disabled:busy},"Сохранить выбор")));
  return panel;
}
function render() {
  const header=node("header",{},paragraph("LANGUAGE TUTOR","brand"),node("h1",{},"Английский в контексте"),paragraph(state.title??"YouTube · общий учебный профиль","video-title"),
    paragraph(state.video_id?`Видео ${state.video_id} · вкладка ${tabId}`:"Обычное видео YouTube","tab-id"));
  const nav=node("nav",{"aria-label":"Разделы панели"});
  for(const [key,label]of[["material","Видео"],["dictionary","Мои слова"],["diagnostics","Диагностика"],["settings","Настройки"]])nav.append(node("button",{type:"button","aria-current":view===key?"page":"false",onClick:()=>{
    view=key;error="";notice="";render();if(key==="dictionary")void action(()=>loadDictionary());if(key==="diagnostics")void action(()=>loadDiagnostic());if(key==="settings")void loadAISettings();
  }},label));
  const body=view==="dictionary"?dictionaryView():view==="diagnostics"?diagnosticView():view==="settings"?settingsView():materialView();
  app.replaceChildren(header,nav);
  if(error)app.append(node("p",{class:"warning",role:"alert"},error));
  if(notice)app.append(node("p",{class:"success",role:"status"},notice));
  if(busy)app.append(node("p",{class:"help",role:"status"},"Выполняем запрос…"));app.append(body);
  app.append(node("div",{class:"links"},node("a",{href:backend,target:"_blank",rel:"noreferrer"},"Общий профиль в приложении ↗")));
}
async function backendChanged(value) {
  let next;
  try{next=backendUrl(value);}catch{return;}
  if(next===backend)return;
  const wasEnabled=state.enabled;
  backend=next;aiSettingsRequest++;
  backendResetPending=true;
  aiSettings=null;aiModels=[];aiDraft=null;aiSettingsLoading=false;aiSettingsError="";
  dictionary=[];dictionaryLoaded=false;filters={search:"",score:"",ignored:"false"};
  diagnostic=null;diagnosticLoaded=false;drafts.clear();videoExpandedId=null;videoFilters={search:"",score:"",ignored:""};
  chat=null;chatOpen=false;chatBusy=false;chatError="";question="";retry=null;cardAbort?.abort();
  acceptState({status:"idle",enabled:false,video_id:null,selection:null,message:"Локальный сервер изменён. Переподключите учебный режим к новому профилю."});
  error="";notice="";render();
  if(!wasEnabled)return;
  try{
    if(!await chrome.permissions.contains({origins:[permissionOrigin(next)]}))return;
    backendResetPending=false;
    await message("lt:start");
  }catch{}
}
chrome.runtime.onMessage.addListener((message,sender)=>{
  if(sender.id===chrome.runtime.id&&sender.tab?.id===tabId&&["lt:state","lt:selection"].includes(message.type)){
    if(backendResetPending)return;
    if(message.type==="lt:selection")view="material";acceptState(message.state);
  }
});
chrome.tabs.onActivated.addListener(()=>void syncTab());
chrome.tabs.onUpdated.addListener((id,change)=>{if(id===tabId&&change.status==="complete")void syncTab();});
chrome.storage.onChanged.addListener((changes,area)=>{if(area==="local"&&changes.backend)void backendChanged(changes.backend.newValue);});
try{backend=backendUrl((await chrome.storage.local.get({backend:DEFAULT_BACKEND})).backend);}catch{backend=DEFAULT_BACKEND;}
await syncTab();
