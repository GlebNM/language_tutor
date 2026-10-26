import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import { Icon, Modal } from "./UI";
import "./textGeneration.css";

type GenerationRequest = {
  topic: string | null;
  style: string | null;
  length: "short" | "medium" | "long";
  difficulty: "gentle" | "normal" | "rich";
};
type GenerationIssue = {
  criterion_id?: string;
  explanation?: string;
  message?: string;
  repair_instructions?: string;
};
type GenerationJob = {
  id: string;
  status: string;
  request: GenerationRequest;
  revision: number;
  error?: string | null;
  draft?: { title: string; paragraphs: Array<{ text: string }> } | null;
  feedback?: { blocking_issues?: GenerationIssue[] } | null;
  result_material_id?: string | null;
};
const stages: Record<string, string> = {
  queued: "В очереди",
  preparing: "Подбираем контекст",
  writing: "Пишем текст",
  checking: "Проверяем текст",
  revising: "Дорабатываем текст",
  saving: "Сохраняем текст",
  completed: "Текст готов",
  needs_changes: "Текст требует доработки",
  failed: "Не удалось создать текст",
  cancelled: "Генерация остановлена",
  interrupted: "Генерация прервалась",
};
const activeStages = new Set([
  "queued",
  "preparing",
  "writing",
  "checking",
  "revising",
  "saving",
]);
const difficultyLimits = {
  gentle: [4, 1],
  normal: [7, 3],
  rich: [10, 5],
} as const;

export function TextGeneration({
  onOpen,
  onCompleted,
  onSettings,
}: {
  onOpen(id: string): void;
  onCompleted(): void;
  onSettings(): void;
}) {
  const [open, setOpen] = useState(false);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [jobs, setJobs] = useState<GenerationJob[]>([]);
  const [topic, setTopic] = useState("");
  const [style, setStyle] = useState("");
  const [length, setLength] = useState<GenerationRequest["length"]>("medium");
  const [difficulty, setDifficulty] =
    useState<GenerationRequest["difficulty"]>("gentle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const submission = useRef<{ signature: string; key: string } | null>(null);
  const reported = useRef(new Set<string>());
  const completion = useRef(onCompleted);
  completion.current = onCompleted;
  const action = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      try {
        const [settings, response] = await Promise.all([
          api<{ available: boolean }>("/text-generation/settings", {
            signal: controller.signal,
          }),
          api<{ jobs: GenerationJob[] }>("/text-generation/jobs", {
            signal: controller.signal,
          }),
        ]);
        if (controller.signal.aborted) return;
        setAvailable(settings.available);
        setJobs(response.jobs);
        for (const job of response.jobs) {
          if (job.status === "completed" && !reported.current.has(job.id)) {
            reported.current.add(job.id);
            completion.current();
          }
        }
        if (response.jobs.some((job) => activeStages.has(job.status)))
          timer = window.setTimeout(() => void poll(), 1500);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [attempt, open]);
  function update(job: GenerationJob) {
    setJobs((current) => [
      job,
      ...current.filter((value) => value.id !== job.id),
    ]);
    setAttempt((value) => value + 1);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (action.current || !available) return;
    action.current = true;
    setBusy(true);
    setError("");
    const request: GenerationRequest = {
      topic: topic.trim() || null,
      style: style.trim() || null,
      length,
      difficulty,
    };
    const signature = JSON.stringify(request);
    if (submission.current?.signature !== signature)
      submission.current = { signature, key: crypto.randomUUID() };
    try {
      const job = await api<GenerationJob>("/text-generation/jobs", {
        method: "POST",
        headers: { "Idempotency-Key": submission.current.key },
        body: signature,
      });
      submission.current = null;
      update(job);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      action.current = false;
      setBusy(false);
    }
  }
  async function jobAction(job: GenerationJob, retry: boolean) {
    if (action.current) return;
    action.current = true;
    setBusy(true);
    setError("");
    try {
      update(
        await api<GenerationJob>(
          `/text-generation/jobs/${job.id}${retry ? "/retry" : ""}`,
          {
            method: retry ? "POST" : "DELETE",
            ...(retry ? { body: "{}" } : {}),
          },
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      action.current = false;
      setBusy(false);
    }
  }
  const running = jobs.some((job) => activeStages.has(job.status));
  const limits = difficultyLimits[difficulty];
  return (
    <section className="text-generation" aria-label="Генерация текстов">
      <button
        className="secondary"
        onClick={() => {
          setError("");
          setOpen(true);
        }}
      >
        <Icon name="plus" size={17} />
        Создать текст
      </button>
      {!open && error && (
        <p className="warning" role="alert">
          {error}{" "}
          <button
            className="quiet"
            onClick={() => {
              setError("");
              setAttempt((value) => value + 1);
            }}
          >
            Повторить
          </button>
        </p>
      )}
      {!!jobs.length && (
        <div className="generation-jobs">
          {jobs.slice(0, 3).map((job) => {
            const active = activeStages.has(job.status);
            const unsuccessful = [
              "needs_changes",
              "failed",
              "interrupted",
              "cancelled",
            ].includes(job.status);
            return (
              <article
                className="generation-job"
                key={job.id}
                aria-label={job.draft?.title ?? "Создание текста"}
              >
                <div className="generation-job-heading">
                  <strong>
                    {job.draft?.title || job.request.topic || "Новый текст"}
                  </strong>
                  <span role="status">
                    {stages[job.status] ?? job.status}
                    {active && job.revision > 1
                      ? ` · версия ${job.revision}`
                      : ""}
                  </span>
                </div>
                {active && <progress aria-label="Генерация текста" />}
                {job.error && <p className="warning">{job.error}</p>}
                {job.feedback?.blocking_issues?.length ? (
                  <ul className="generation-issues">
                    {job.feedback.blocking_issues.map((issue, index) => (
                      <li key={index}>
                        {issue.repair_instructions ||
                          issue.explanation ||
                          issue.message ||
                          issue.criterion_id ||
                          "Нужно доработать текст"}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {unsuccessful && job.draft && (
                  <details className="generation-draft">
                    <summary>Посмотреть черновик</summary>
                    {job.draft.paragraphs.map((paragraph, index) => (
                      <p key={index}>{paragraph.text}</p>
                    ))}
                  </details>
                )}
                <div className="actions">
                  {active && (
                    <button
                      className="quiet"
                      disabled={busy}
                      onClick={() => void jobAction(job, false)}
                    >
                      Остановить
                    </button>
                  )}
                  {unsuccessful && (
                    <button
                      className="secondary"
                      disabled={busy || running || !available}
                      onClick={() => void jobAction(job, true)}
                    >
                      Попробовать снова
                    </button>
                  )}
                  {job.status === "completed" && job.result_material_id && (
                    <button onClick={() => onOpen(job.result_material_id!)}>
                      Читать текст
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {open && (
        <Modal
          labelledBy="generation-title"
          onClose={() => setOpen(false)}
          busy={busy}
        >
          <div className="panel-title">
            <span />
            <button
              className="quiet close"
              aria-label="Закрыть генерацию"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              ×
            </button>
          </div>
          <h2 id="generation-title">Создать текст</h2>
          <form onSubmit={(event) => void submit(event)}>
            <label>
              Тема (необязательно)
              <input
                maxLength={300}
                value={topic}
                disabled={busy}
                onChange={(event) => setTopic(event.target.value)}
                placeholder="Например, прогулка по городу"
              />
            </label>
            <label>
              Стиль (необязательно)
              <input
                maxLength={300}
                value={style}
                disabled={busy}
                onChange={(event) => setStyle(event.target.value)}
                placeholder="Например, лёгкий разговорный"
              />
            </label>
            <label>
              Длина
              <select
                value={length}
                disabled={busy}
                onChange={(event) =>
                  setLength(event.target.value as GenerationRequest["length"])
                }
              >
                <option value="short">Короткий · 150–200 слов</option>
                <option value="medium">Средний · 250–350 слов</option>
                <option value="long">Длинный · 450–550 слов</option>
              </select>
            </label>
            <label>
              Сложность
              <select
                value={difficulty}
                disabled={busy}
                onChange={(event) =>
                  setDifficulty(
                    event.target.value as GenerationRequest["difficulty"],
                  )
                }
              >
                <option value="gentle">Бережная</option>
                <option value="normal">Обычная</option>
                <option value="rich">Насыщенная</option>
              </select>
            </label>
            <p className="help">
              Изучаемые слова — до {limits[0]}%, незнакомые и непроверенные — до{" "}
              {limits[1]}% текста.
            </p>
            {available === false && (
              <p className="warning">
                Для генерации подключите свою подписку ChatGPT.{" "}
                <button
                  type="button"
                  className="quiet"
                  onClick={() => {
                    setOpen(false);
                    onSettings();
                  }}
                >
                  Открыть настройки
                </button>
              </p>
            )}
            {available === null && <p role="status">Проверяем подключение…</p>}
            {running && (
              <p className="help">
                Сейчас создаётся другой текст. Дождитесь результата или
                остановите его.
              </p>
            )}
            {error && (
              <p className="warning" role="alert">
                {error}
              </p>
            )}
            <button className="submit" disabled={busy || !available || running}>
              {busy ? "Запускаем…" : "Создать текст"}
            </button>
          </form>
        </Modal>
      )}
    </section>
  );
}
