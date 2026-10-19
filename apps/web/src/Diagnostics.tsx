import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";

type Item = {id: string;kind: string;group: string | null;prompt: string};
type Answer = {item_id: string; answer: string; action: string};
type Session = {id: string; status: string; items: Item[]; answers: Answer[]; summary?: unknown};
type State = {session: Session | null; profile?: unknown};

export function Diagnostics({onApplied}: {onApplied(): void}) {
  const [state,setState] = useState<State>({session:null});
  const [busy,setBusy] = useState(false);const [error,setError] = useState("");
  const [answer,setAnswer] = useState("");
  const session = state.session;
  const completedIds = new Set(session?.answers.map(a => a.item_id) ?? []);
  const item = session?.items.find(i => !completedIds.has(i.id));
  useEffect(() => {void api<State>("/diagnostics").then(setState).catch(e => setError(e.message));},[]);
  async function action(path: string, body: object = {}) {
    setBusy(true);setError("");
    try {
      await api(path,{method:"POST",body:JSON.stringify(body)});
      setState(await api<State>("/diagnostics"));setAnswer("");onApplied();
    } catch(e) {setError((e as Error).message);}
    finally {setBusy(false);}
  }
  function respond(event: FormEvent, kind="answer") {
    event.preventDefault();
    if(!session || !item) return;
    void action(`/diagnostics/${session.id}/answers`,{item_id:item.id,answer:kind === "answer" ? answer : "",action:kind});
  }
  const active = session?.status === "in_progress" || session?.status === "active";
  return <section className="diagnostic"><p className="eyebrow">НАЧАЛЬНЫЙ ПРОФИЛЬ</p><h1>Знакомимся с вашим английским</h1>
    <p className="lede">12 слов и две короткие фразы, примерно 3–5 минут. Объясняйте значение по-русски своими словами.</p>
    <div className="settings-card"><p>Небольшая выборка помогает предположить знакомую лексику. Это эвристика, а не сертифицированный уровень CEFR. Ручные оценки слов сохранятся.</p>
      {error && <p className="warning" role="alert">{error}</p>}
      {!active && <>
        {session?.status === "completed" && <><h2>Профиль обновлён</h2><p>Проверенные слова и предположения по частотным группам применены. В словаре и карточках указан источник каждой оценки.</p><DiagnosticSummary summary={session.summary} /></>}
        <div className="actions"><button disabled={busy} onClick={() => void action("/diagnostics")}>{busy ? "Готовим задания…" : session ? "Пройти заново" : "Начать диагностику"}</button><a href="#" onClick={e => {e.preventDefault();window.location.reload();}}>Начать без оценки</a></div>
      </>}
      {active && session && <>
        <p className="eyebrow">ОТВЕЧЕНО {session.answers.length} ИЗ {session.items.length}</p>
        <progress max={session.items.length} value={session.answers.length} />
        {item ? <form onSubmit={event => respond(event)}><p className="help">{item.kind === "word" ? "Объясните значение слова" : "Как вы понимаете эту фразу?"}</p><h2 className="diagnostic-prompt">{item.prompt}</h2>
          <label>Ваш ответ<textarea required maxLength={2000} value={answer} onChange={e => setAnswer(e.target.value)} placeholder="Значение или короткое объяснение…" /></label>
          <div className="actions"><button disabled={busy || !answer.trim()}>{busy ? "Сохраняем…" : "Ответить"}</button><button type="button" className="secondary" disabled={busy} onClick={event => respond(event,"dont_know")}>Не знаю</button><button type="button" className="quiet" disabled={busy} onClick={event => respond(event,"skip")}>Пропустить слово</button></div>
        </form> : <><h2>Все ответы сохранены</h2><p>Агент проверит ответы и составит предварительный профиль.</p><button disabled={busy} onClick={() => void action(`/diagnostics/${session.id}/complete`)}>{busy ? "Агент проверяет ответы…" : error ? "Повторить проверку" : "Завершить диагностику"}</button></>}
        <button className="quiet" disabled={busy} onClick={() => void action(`/diagnostics/${session.id}/skip`)}>Продолжить без завершения диагностики</button>
        <p className="help">Прогресс сохраняется. Незавершённая диагностика не применяет предположения ко всему словарю.</p>
      </>}
    </div>
  </section>;
}
function DiagnosticSummary({summary}: {summary: unknown}) {
  if(typeof summary === "string") return <p>{summary}</p>;
  if(summary && typeof summary === "object" && "description" in summary) {
    const result = summary as {description: string; model?: string | null; reasoning_effort?: string | null; groups?: {group: number; correct: number; total: number; inferred: boolean}[]; grades?: {item_id: string; correct: boolean | null; explanation: string}[]};
    const reasoning = Object.prototype.hasOwnProperty.call(result, "reasoning_effort")
      ? result.reasoning_effort ?? "по умолчанию"
      : "не записан";
    return <><p>{result.description}</p><h3>Предположения по группам</h3>
      {result.model && <p className="ai-provenance">Модель: {result.model} · reasoning: {reasoning}</p>}
      {result.groups?.map(group => <p key={group.group}>Группа {group.group}: {group.correct} из {group.total} верно. {group.inferred ? "Для непроверенных слов предполагаем «Узнаю в контексте»." : "Непроверенные слова остаются без оценки."}</p>)}
      <details><summary>Проверенные ответы</summary>{result.grades?.map(grade => <p key={grade.item_id}><strong>{grade.item_id}: {grade.correct === null ? "пропущено" : grade.correct ? "верно" : "пока незнакомо"}</strong><br />{grade.explanation}</p>)}</details>
    </>;
  }
  return null;
}
