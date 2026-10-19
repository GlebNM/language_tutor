import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, time } from "./api";
import type { Difficulty, Knowledge, Selection, Token } from "./api";

export const scores = ["Не знаю", "Учу", "Узнаю в контексте", "Уверенно знаю"];
export function knowledgeLabel(knowledge?: Knowledge) {
  if (knowledge?.ignored) return "Исключено из оценки";
  return knowledge?.score == null ? "Пока не оценено" : scores[knowledge.score];
}

export function FamiliarityEditor({ token, onSaved }: { token: Token; onSaved(): void }) {
  const [score, setScore] = useState(String(token.knowledge?.score ?? ""));
  const [ignored, setIgnored] = useState(token.knowledge?.ignored ?? false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setScore(String(token.knowledge?.score ?? ""));
    setIgnored(token.knowledge?.ignored ?? false);
    setDirty(false);
    setError("");
  }, [token.id, token.lexeme_id, token.knowledge?.score, token.knowledge?.ignored]);
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api(`/lexemes/${token.lexeme_id}/knowledge`, {
        method: "PUT", body: JSON.stringify({score: score === "" ? null : Number(score), ignored}),
      });
      setDirty(false); onSaved();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <form className="knowledge-editor" onSubmit={event => void save(event)}>
    <label>Знакомость
      <select value={score} onChange={e => {setScore(e.target.value); setDirty(true);}}>
        <option value="">Пока не оценено</option>
        {scores.map((label, value) => <option key={value} value={value}>{label}</option>)}
      </select>
    </label>
    <label className="checkbox"><input type="checkbox" checked={ignored} onChange={e => {setIgnored(e.target.checked); setDirty(true);}} />Исключить из оценки</label>
    {token.knowledge?.provisional && <p className="help">Предварительная оценка · {token.knowledge.source === "prior" ? "предположение по частотной группе" : "ответ в диагностике"}</p>}
    {dirty && <p className="warning" role="status">Изменения не сохранены</p>}
    {error && <p className="warning" role="alert">{error}</p>}
    <button className="secondary" disabled={busy || !dirty}>{busy ? "Сохраняем…" : error ? "Повторить сохранение" : "Сохранить знакомость"}</button>
  </form>;
}

export function DifficultyPanel({ value }: { value?: Difficulty }) {
  if (!value) return null;
  const number = (key: string) => typeof value[key] === "number" ? Number(value[key]) : null;
  const familiar = number("known_percent");
  const unfamiliar = number("unfamiliar_percent");
  const unassessed = number("unassessed_percent");
  return <section className="difficulty-card" aria-label="Сложность материала">
    <div className="panel-title"><h2>Лексика материала</h2>{value.provisional === true && <span className="badge">Предварительно</span>}</div>
    {familiar != null ? <>
      <div className="coverage-bar" aria-hidden="true"><span style={{width:`${familiar}%`}} /><i style={{width:`${unfamiliar ?? 0}%`}} /></div>
      <p><strong>{familiar.toFixed(1)}%</strong> знакомо · {unfamiliar?.toFixed(1)}% незнакомо · {unassessed?.toFixed(1)}% пока не оценено</p>
    </> : null}
    {typeof value.label === "string" && <strong>{value.label}</strong>}
    {typeof value.unique_unfamiliar === "number" && <p>Незнакомых базовых форм: {value.unique_unfamiliar}</p>}
    {Array.isArray(value.coverage_interval) && <p>Возможное покрытие с учётом неоценённых слов: {Number(value.coverage_interval[0]).toFixed(1)}–{Number(value.coverage_interval[1]).toFixed(1)}%.</p>}
    {Number(value.provisional_percent) > 0 && <p>Предварительные оценки: {Number(value.provisional_percent).toFixed(1)}% вхождений; {Number(value.provisional_known_percent).toFixed(1)} п. п. знакомой лексики основаны на предположении.</p>}
    {Array.isArray(value.limitations) && value.limitations.map((limitation, index) => <p key={index}>{String(limitation)}</p>)}
    <p className="help">Оцениваем лексику: скорость речи, акцент и грамматика влияют на понимание отдельно.</p>
  </section>;
}

type CardResult = {card: {translation: string; explanation: string; expression: string; example: string}; duration_ms: number};
type Citation = {material_id: string; cue_id?: string; title?: string; text: string; start_ms: number; youtube_url?: string};
type ChatAnswer = {answer: string; educational_example?: string | null; citations?: Citation[]};
type Turn = {request_id: string;message: string;response: ChatAnswer | null};
type Conversation = {id: string; token_id?: string; messages: Turn[]};

export function LearningPanel({selection, onSaved, onSeek, onOpen}: {
  selection: Selection; onSaved(): void; onSeek(milliseconds: number): void; onOpen(id: string): void;
}) {
  const [card, setCard] = useState<CardResult | null>(null);
  const [cardError, setCardError] = useState("");
  const [cardBusy, setCardBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [chat, setChat] = useState<Conversation | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [chatError, setChatError] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const retry = useRef<{message: string; request_id: string} | null>(null);
  const generation = useRef(0);
  const [lemma, setLemma] = useState(selection.token.lemma);
  const [pos, setPos] = useState(selection.token.part_of_speech);
  const [correctionError, setCorrectionError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setCard(null); setCardBusy(true); setCardError("");
    api<CardResult>("/cards", {method:"POST", signal:controller.signal, body:JSON.stringify({material_id:selection.material_id,cue_id:selection.cue.id,token_id:selection.token.id})})
      .then(setCard).catch(e => {if(e.name !== "AbortError") setCardError(e.message);})
      .finally(() => {if(!controller.signal.aborted) setCardBusy(false);});
    return () => controller.abort();
  }, [selection.material_id, selection.cue.id, selection.token.id, selection.token.lexeme_id, attempt]);
  useEffect(() => {
    ++generation.current; setChat(null); setChatOpen(false); setChatError(""); setChatBusy(false); retry.current = null;
    setLemma(selection.token.lemma); setPos(selection.token.part_of_speech); setCorrectionError("");
    return () => {++generation.current;};
  }, [selection.token.id]);
  useEffect(() => {
    const controller = new AbortController();
    api<Conversation[]>(`/conversations?material_id=${selection.material_id}`,{signal:controller.signal})
      .then(async conversations => {
        const previous = conversations.find(item => item.token_id === selection.token.id);
        if(previous) {
          const saved = await api<Conversation>(`/conversations/${previous.id}`,{signal:controller.signal});
          if(!controller.signal.aborted) setChat(saved);
        }
      }).catch(() => {});
    return () => controller.abort();
  }, [selection.material_id,selection.token.id]);
  async function send(event: FormEvent) {
    event.preventDefault();
    const message = question.trim(); if (!message) return;
    const version = generation.current;
    const payload = retry.current?.message === message ? retry.current : {message, request_id:crypto.randomUUID()};
    retry.current = payload; setChatBusy(true); setChatError("");
    try {
      const current = chat ?? await api<Conversation>("/conversations", {method:"POST", body:JSON.stringify({material_id:selection.material_id,cue_id:selection.cue.id,token_id:selection.token.id})});
      if (version !== generation.current) return;
      setChat(current);
      await api<ChatAnswer>(`/conversations/${current.id}/messages`, {method:"POST", body:JSON.stringify(payload)});
      const result = await api<Conversation>(`/conversations/${current.id}`);
      if (version === generation.current) {setChat(result); setQuestion(""); retry.current=null;}
    } catch(e) {if(version === generation.current) setChatError((e as Error).message);}
    finally {if(version === generation.current) setChatBusy(false);}
  }
  async function correct(event: FormEvent) {
    event.preventDefault(); setCorrectionError("");
    try {await api(`/materials/${selection.material_id}/occurrences/${selection.token.id}/lexeme`, {method:"PUT",body:JSON.stringify({cue_id:selection.cue.id,lemma,part_of_speech:pos})});onSaved();}
    catch(e) {setCorrectionError((e as Error).message);}
  }
  return <>
    <div className="contextual-card">
      {cardBusy && <p role="status">Разбираем слово в контексте…</p>}
      {cardError && <div role="alert"><p className="warning">{cardError}</p><button className="secondary" onClick={() => setAttempt(attempt+1)}>Повторить разбор</button></div>}
      {card && <><h3>{card.card.translation}</h3><p>{card.card.explanation}</p><p className="expression">{card.card.expression}</p><p className="example">{card.card.example}</p></>}
    </div>
    <FamiliarityEditor token={selection.token} onSaved={onSaved} />
    <details><summary>Исправить базовую форму</summary><form onSubmit={e => void correct(e)} className="lemma-form">
      <label>Базовая форма<input required maxLength={100} value={lemma} onChange={e => setLemma(e.target.value)} /></label>
      <label>Часть речи<select value={pos} onChange={e => setPos(e.target.value)}>{["NOUN","VERB","ADJ","ADV","PROPN","AUX","PRON","ADP","DET","CCONJ","SCONJ","PART","INTJ","NUM","X"].map(p => <option key={p}>{p}</option>)}</select></label>
      {correctionError && <p className="warning">{correctionError}</p>}<button className="secondary">Сохранить форму</button>
    </form></details>
    <button className="discuss" onClick={() => setChatOpen(!chatOpen)}>Обсудить с агентом</button>
    {chatOpen && <section className="chat"><h3>Учебный агент</h3>
      <p className="help">Спросите о значении или попросите найти слово в предыдущих материалах.</p>
      {chat?.messages?.map(turn => <div key={turn.request_id} className="chat-turn"><div className="chat-message user"><small>Вы</small><p>{turn.message}</p></div>
        {turn.response && <div className="chat-message assistant"><small>Агент</small><p>{turn.response.answer}</p>
        {turn.response.educational_example && <p className="example">Учебный пример: {turn.response.educational_example}</p>}
        {turn.response.citations?.map((citation, i) => <blockquote key={i}>{citation.text}<button className="timestamp" onClick={() => citation.material_id === selection.material_id ? onSeek(citation.start_ms) : onOpen(citation.material_id)}>{citation.title} · {time(citation.start_ms)} ↗</button>{citation.youtube_url && <a href={citation.youtube_url} target="_blank" rel="noreferrer">На YouTube ↗</a>}</blockquote>)}
        </div>}
      </div>)}
      <form onSubmit={event => void send(event)}><label>Ваш вопрос<textarea required maxLength={4000} value={question} onChange={e => setQuestion(e.target.value)} placeholder="Почему здесь такое значение?" /></label>
      {chatError && <p role="alert" className="warning">{chatError}</p>}
      <button disabled={chatBusy || !question.trim()}>{chatBusy ? "Агент отвечает…" : chatError ? "Повторить вопрос" : "Отправить"}</button></form>
    </section>}
  </>;
}

type DictionaryEntry = Token & {id: string; knowledge: Knowledge};
export function Dictionary() {
  const [entries, setEntries] = useState<DictionaryEntry[]>([]);
  const [search,setSearch] = useState(""); const [score,setScore] = useState(""); const [ignored,setIgnored] = useState("");
  const [revision,setRevision] = useState(0); const [error,setError] = useState(""); const [busy,setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();setBusy(true);
    const params = new URLSearchParams({search}); if(score) params.set("score",score); if(ignored) params.set("ignored",ignored);
    api<DictionaryEntry[]>(`/dictionary?${params}`, {signal:controller.signal}).then(items => {setEntries(items);setError("");})
      .catch(e => {if(e.name !== "AbortError") setError(e.message);}).finally(() => {if(!controller.signal.aborted) setBusy(false);});
    return () => controller.abort();
  }, [search,score,ignored,revision]);
  return <section className="dictionary"><p className="eyebrow">ЛИЧНЫЙ СЛОВАРЬ</p><h1>Мои слова</h1><p className="lede">Ручные оценки сохраняются и имеют приоритет над диагностикой.</p>
    <div className="dictionary-filters"><label>Поиск<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Базовая форма слова" /></label>
    <label>Знакомость<select value={score} onChange={e => setScore(e.target.value)}><option value="">Все оценки</option><option value="unassessed">Пока не оценено</option>{scores.map((s,i)=><option key={i} value={i}>{s}</option>)}</select></label>
    <label>Исключённые<select value={ignored} onChange={e => setIgnored(e.target.value)}><option value="">Все слова</option><option value="false">Учитываемые</option><option value="true">Исключённые</option></select></label></div>
    {error && <p role="alert" className="warning">{error}<button onClick={() => setRevision(revision+1)}>Повторить</button></p>}
    {busy && <p role="status">Обновляем словарь…</p>}
    {!busy && !entries.length && <p>Пока нет подходящих слов. Откройте материал и оцените слово в субтитрах.</p>}
    <div className="dictionary-grid">{entries.map(entry => <article className="dictionary-entry" key={entry.lexeme_id ?? entry.id}><h2>{entry.lemma} <small>{entry.part_of_speech}</small></h2><p>{knowledgeLabel(entry.knowledge)}</p><FamiliarityEditor token={{...entry,lexeme_id:entry.lexeme_id ?? entry.id}} onSaved={() => setRevision(revision+1)} /></article>)}</div>
  </section>;
}
