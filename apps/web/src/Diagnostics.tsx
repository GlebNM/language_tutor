import { useEffect, useState } from "react";
import { api } from "./api";

type Item = {
  id: string;
  prompt: string;
  surface?: string;
  start_offset?: number;
  end_offset?: number;
};
type Choice = "known" | "unsure" | "unknown" | "skip";
type Session = {
  id: string;
  status: string;
  format_version: number;
  min_questions: number;
  max_questions: number;
  ready_to_complete: boolean;
  items: Item[];
  answers: { item_id: string; action: string }[];
  summary?: unknown;
};
type State = { session: Session | null };

export function Diagnostics({ onApplied }: { onApplied(): void }) {
  const [state, setState] = useState<State>({ session: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const session = state.session;
  const completedIds = new Set(session?.answers.map((a) => a.item_id) ?? []);
  const item = session?.items.find((i) => !completedIds.has(i.id));
  const active = session?.status === "in_progress" && session.format_version === 2;

  useEffect(() => {
    const controller = new AbortController();
    void api<State>("/diagnostics", { signal: controller.signal })
      .then(setState)
      .catch((e: Error) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, []);

  async function action(path: string, body: object = {}, advance = false) {
    setBusy(true);
    setError("");
    try {
      let next = await api<Session>(path, { method: "POST", body: JSON.stringify(body) });
      setState({ session: next });
      if (advance && next.status === "in_progress" && !next.ready_to_complete) {
        next = await api<Session>(`/diagnostics/${next.id}/next`, {
          method: "POST", body: "{}",
        });
        setState({ session: next });
      }
      if (next.status === "completed") onApplied();
    } catch (e) {
      setError((e as Error).message);
      // The answer may have been saved before generation failed. Resume from the server.
      try {
        setState(await api<State>("/diagnostics"));
      } catch { /* Keep the last saved screen when the connection is unavailable. */ }
    } finally {
      setBusy(false);
    }
  }

  function respond(choice: Choice) {
    if (!session || !item) return;
    void action(`/diagnostics/${session.id}/answers`, {
      item_id: item.id, action: choice,
    }, true);
  }

  return (
    <section className="diagnostic" aria-busy={busy}>
      <h1>Проверка словаря</h1>
      <p className="lede">
        Короткие предложения со словами разной сложности. Отмечайте, знаете ли
        выделенное слово в этом значении. Примерно 3–5 минут.
      </p>
      <div className="settings-card">
        {error && <p className="warning" role="alert">{error}</p>}
        {!active && (
          <>
            {session?.status === "completed" ? (
              <>
                <h2>Профиль обновлён</h2>
                <DiagnosticSummary summary={session.summary} />
              </>
            ) : (
              <p>
                Агент подберёт от 12 до 18 слов по вашим ответам. Результат —
                предварительная оценка знакомости; ручные оценки имеют приоритет.
              </p>
            )}
            {session?.status === "in_progress" && session.format_version !== 2 && (
              <p>Формат обновился: теперь можно отвечать кнопками. Начните новую диагностику.</p>
            )}
            <div className="actions">
              <button disabled={busy} onClick={() => void action("/diagnostics", {}, true)}>
                {busy ? "Готовим предложение…" : session ? "Пройти заново" : "Начать диагностику"}
              </button>
              <a href="#materials">Начать без оценки</a>
            </div>
          </>
        )}
        {active && session && (
          <>
            <p className="eyebrow">
              Ответов: {session.answers.length} · до {session.max_questions} вопросов
            </p>
            <progress
              aria-label="Ответы в диагностике"
              max={session.max_questions}
              value={session.ready_to_complete ? session.max_questions : session.answers.length}
            />
            <div aria-live="polite" aria-atomic="true">
              {item ? (
                <>
                  <p className="help">Знаете значение выделенного слова здесь?</p>
                  <h2 className="diagnostic-prompt" lang="en">
                    {item.prompt.slice(0, item.start_offset)}
                    <mark>{item.prompt.slice(item.start_offset, item.end_offset)}</mark>
                    {item.prompt.slice(item.end_offset)}
                  </h2>
                  <div className="actions diagnostic-choices">
                    <button disabled={busy} onClick={() => respond("known")}>Знаю</button>
                    <button className="secondary" disabled={busy} onClick={() => respond("unsure")}>
                      Не уверен
                    </button>
                    <button className="secondary" disabled={busy} onClick={() => respond("unknown")}>
                      Не знаю
                    </button>
                    <button className="quiet" disabled={busy} onClick={() => respond("skip")}>
                      Пропустить слово
                    </button>
                  </div>
                </>
              ) : session.ready_to_complete ? (
                <>
                  <h2>Ответов достаточно</h2>
                  <p>Сохраните предварительный профиль, чтобы учитывать его при выборе материалов.</p>
                  <button disabled={busy}
                    onClick={() => void action(`/diagnostics/${session.id}/complete`)}>
                    {busy ? "Сохраняем…" : "Сохранить результат"}
                  </button>
                </>
              ) : (
                <>
                  <p role="status">
                    {busy ? "Агент подбирает слово и проверяет предложение…" : "Продолжим с нового предложения."}
                  </p>
                  {!busy && (
                    <button onClick={() => void action(`/diagnostics/${session.id}/next`)}>
                      {error ? "Повторить подготовку" : "Продолжить диагностику"}
                    </button>
                  )}
                </>
              )}
            </div>
            <div className="actions">
              <a href="#materials">Продолжить позже</a>
              <button className="quiet" disabled={busy}
                onClick={() => void action(`/diagnostics/${session.id}/skip`)}>
                Завершить без результата
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

const choices: Record<string, string> = {
  known: "Знаю", unsure: "Не уверен", unknown: "Не знаю", skip: "Пропущено",
};

function DiagnosticSummary({ summary }: { summary: unknown }) {
  if (typeof summary === "string") return <p>{summary}</p>;
  if (!summary || typeof summary !== "object" || !("description" in summary)) return null;
  const result = summary as {
    description: string;
    basis?: string;
    notice?: string;
    model?: string;
    reasoning_effort?: string | null;
    groups?: { group: number; known?: number; correct?: number; total: number; inferred: boolean }[];
    grades?: {
      item_id: string; word?: string; prompt?: string; meaning?: string;
      action?: string; correct?: boolean | null; explanation?: string;
    }[];
  };
  return (
    <>
      <p>{result.description}</p>
      {result.notice && <p className="help">{result.notice}</p>}
      <details>
        <summary>Ответы и предварительные оценки</summary>
        {result.groups?.map((group) => (
          <p key={group.group}>
            Группа {group.group}: {result.basis === "self_report"
              ? `${group.known} из ${group.total} — «Знаю».`
              : `${group.correct} из ${group.total} верно.`}{" "}
            {group.inferred ? "Для остальных слов предполагаем «Узнаю в контексте»."
              : "Для остальных слов нет предположения о знакомости."}
          </p>
        ))}
        {result.grades?.map((grade) => (
          <p key={grade.item_id}>
            <strong>{grade.word ?? grade.item_id}: {grade.action
              ? choices[grade.action] ?? grade.action
              : grade.correct === null ? "пропущено" : grade.correct ? "верно" : "пока незнакомо"}</strong>
            {grade.prompt && <><br /><span lang="en">{grade.prompt}</span></>}
            {(grade.meaning || grade.explanation) && <><br />{grade.meaning ?? grade.explanation}</>}
          </p>
        ))}
        {result.model && (
          <p className="ai-provenance">
            Модель: {result.model} · reasoning: {result.reasoning_effort ?? "по умолчанию"}
          </p>
        )}
      </details>
    </>
  );
}
