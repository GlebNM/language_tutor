import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, time } from "./api";
import type { Material } from "./api";
import "./discovery.css";

type SearchSettings = {
  configured: boolean;
  source: "saved" | "environment" | null;
  ai_available: boolean;
};
type Difficulty = {
  known_percent: number | null;
  unfamiliar_percent: number | null;
  unassessed_percent: number | null;
  unique_unfamiliar: number;
  label: string | null;
  provisional: boolean;
  limitations: string[];
};
type Candidate = {
  video_id: string;
  title: string;
  channel: string;
  duration_seconds: number | null;
  direct?: boolean;
  status: "pending" | "ready" | "unavailable";
  source?: string;
  error?: string;
  cached?: boolean;
  difficulty?: Difficulty;
  review_words?: string[];
};
type Job = {
  id: string;
  query: string;
  search_query: string;
  status: string;
  checked: number;
  total: number;
  elapsed_seconds: number;
  notice: string;
  error: string | null;
  results: Candidate[];
};
const terminal = (status: string) =>
  ["completed", "cancelled", "failed"].includes(status);
const JOB_KEY = "language-tutor-discovery-job";

export function YouTubeSearchSettings({
  onChanged,
}: {
  onChanged?(value: SearchSettings): void;
}) {
  const [settings, setSettings] = useState<SearchSettings | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api<SearchSettings>("/discovery/settings", { signal: controller.signal })
      .then((value) => {
        setSettings(value);
        onChanged?.(value);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [onChanged]);
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await api<SearchSettings>("/discovery/settings", {
        method: "PUT",
        body: JSON.stringify({ api_key: key.trim() }),
      });
      setSettings(value);
      onChanged?.(value);
      setKey("");
      setNotice("Ключ сохранён. Доступ и квота будут проверены при поиске.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await api<SearchSettings>("/discovery/settings", {
        method: "DELETE",
      });
      setSettings(value);
      onChanged?.(value);
      setKey("");
      setNotice(
        value.configured
          ? "Используется ключ из конфигурации запуска."
          : "Сохранённый ключ удалён.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details
      className="discovery-settings settings-card"
      open={settings?.configured === false ? true : undefined}
    >
      <summary>
        Поиск YouTube {settings?.configured ? "· ключ добавлен" : "· настройка"}
      </summary>
      <p>
        Для поиска нужен ключ YouTube Data API v3. Он сохраняется на этом
        компьютере. Оценка видео по ссылке доступна без ключа.
      </p>
      <p>
        <a
          href="https://console.cloud.google.com/apis/library/youtube.googleapis.com"
          target="_blank"
          rel="noreferrer"
        >
          Включить YouTube Data API v3 ↗
        </a>
        {" · "}
        <a
          href="https://console.cloud.google.com/apis/credentials"
          target="_blank"
          rel="noreferrer"
        >
          Создать ключ ↗
        </a>
      </p>
      <form onSubmit={(event) => void save(event)}>
        <label htmlFor="youtube-search-key">Ключ поиска YouTube</label>
        <div className="discovery-key-row">
          <input
            id="youtube-search-key"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            minLength={10}
            maxLength={200}
            required
            placeholder={
              settings?.configured ? "Ввести новый ключ" : "Вставьте ключ API"
            }
          />
          <button
            className="secondary"
            disabled={busy || key.trim().length < 10}
          >
            {busy ? "Сохраняем…" : "Сохранить ключ"}
          </button>
          {settings?.source === "saved" && (
            <button
              type="button"
              className="quiet"
              disabled={busy}
              onClick={() => void remove()}
            >
              Удалить ключ
            </button>
          )}
        </div>
      </form>
      {settings?.source === "environment" && (
        <p className="help">Ключ задан в конфигурации запуска приложения.</p>
      )}
      {notice && <p role="status">{notice}</p>}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </details>
  );
}

export function Discovery({ onOpen }: { onOpen(id: string): Promise<void> }) {
  const [settings, setSettings] = useState<SearchSettings | null>(null);
  const [query, setQuery] = useState("");
  const [duration, setDuration] = useState("0:1800");
  const [effort, setEffort] = useState("any");
  const [translate, setTranslate] = useState(false);
  const [jobId, setJobId] = useState(
    () => sessionStorage.getItem(JOB_KEY) ?? "",
  );
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  const restoring = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let polling = false;
    let done = false;
    async function poll() {
      if (polling || done) return;
      polling = true;
      try {
        const value = await api<Job>(`/discovery/jobs/${jobId}`, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setJob(value);
          restoring.current = false;
          if (terminal(value.status)) done = true;
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError" && !controller.signal.aborted) {
          if (restoring.current) {
            sessionStorage.removeItem(JOB_KEY);
            setJobId("");
          } else setError((e as Error).message);
        }
      } finally {
        polling = false;
      }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [jobId]);

  const running = job !== null && !terminal(job.status);
  const isLink = /^(?:https?:\/\/|www\.|youtu\.be\/)/i.test(query.trim());
  async function search(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const [minimum, maximum] = duration.split(":").map(Number);
    try {
      const value = await api<Job>("/discovery/jobs", {
        method: "POST",
        body: JSON.stringify({
          query: query.trim(),
          minimum_seconds: minimum,
          maximum_seconds: maximum,
          effort,
          translate_query:
            (translate && settings?.ai_available && !isLink) || false,
        }),
      });
      if (!alive.current) return;
      restoring.current = false;
      sessionStorage.setItem(JOB_KEY, value.id);
      setJob(value);
      setJobId(value.id);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!job) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<Job>(`/discovery/jobs/${job.id}`, {
        method: "DELETE",
      });
      if (alive.current) setJob(value);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  async function openVideo(candidate: Candidate) {
    if (!job) return;
    setOpening(candidate.video_id);
    setError("");
    try {
      const value = await api<Material>(
        `/discovery/jobs/${job.id}/open/${candidate.video_id}`,
        { method: "POST", body: "{}" },
      );
      if (alive.current) await onOpen(value.id);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setOpening("");
    }
  }
  const visible =
    job?.results.filter((candidate) => candidate.status !== "unavailable") ??
    [];
  const unavailable =
    job?.results.filter((candidate) => candidate.status === "unavailable") ??
    [];
  return (
    <section className="discovery">
      <h1>Найти видео</h1>

      <form
        className="discovery-search"
        onSubmit={(event) => void search(event)}
      >
        <label htmlFor="discovery-query">Тема или ссылка на видео</label>
        <div className="discovery-query-row">
          <input
            id="discovery-query"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            minLength={2}
            maxLength={300}
            required
            placeholder="Например: game design, space, cooking…"
          />
          <button
            disabled={busy || running || (!isLink && !settings?.configured)}
          >
            {busy ? "Запускаем…" : isLink ? "Оценить видео" : "Найти видео"}
          </button>
        </div>
        <div className="discovery-filters">
          <label>
            Длительность
            <select
              value={duration}
              onChange={(event) => setDuration(event.target.value)}
              disabled={isLink}
            >
              <option value="0:1800">До 30 минут</option>
              <option value="0:300">До 5 минут</option>
              <option value="300:900">5–15 минут</option>
              <option value="900:1800">15–30 минут</option>
              <option value="0:7200">До 2 часов</option>
            </select>
          </label>
          <label>
            Сначала показать
            <select
              value={effort}
              onChange={(event) => setEffort(event.target.value)}
              disabled={isLink}
            >
              <option value="any">По теме</option>
              <option value="easy">Мало новой лексики</option>
              <option value="some">Понадобятся подсказки</option>
              <option value="challenging">Много новой лексики</option>
            </select>
          </label>
        </div>
        <label className="discovery-translate">
          <input
            type="checkbox"
            checked={translate}
            disabled={!settings?.ai_available || isLink}
            onChange={(event) => setTranslate(event.target.checked)}
          />
          Сформулировать английский запрос через ChatGPT
        </label>
        {!settings?.ai_available && (
          <p className="help">
            Поиск по теме — на английском. Для перевода запроса подключите
            ChatGPT.
          </p>
        )}
        {!settings?.configured && (
          <p className="help">
            Добавьте ключ поиска ниже. По ссылке можно оценить видео уже сейчас.
          </p>
        )}
      </form>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {job && (
        <div className="discovery-job" aria-live="polite">
          <div>
            <strong>
              {
                (
                  {
                    planning: "Формулируем запрос…",
                    searching: "Ищем на YouTube…",
                    analyzing: "Проверяем субтитры…",
                    completed: "Подбор готов",
                    cancelled: "Подбор остановлен",
                    failed: "Подбор не завершён",
                  } as Record<string, string>
                )[job.status]
              }
            </strong>
            <p>
              {job.query}
              {job.search_query && job.query !== job.search_query
                ? ` → ${job.search_query}`
                : ""}
            </p>
            <small>
              {job.checked} из {job.total} проверено ·{" "}
              {job.elapsed_seconds.toFixed(1)} с
            </small>
          </div>
          {running && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void cancel()}
            >
              Остановить
            </button>
          )}
        </div>
      )}
      {job?.error && (
        <p className="warning" role="alert">
          {job.error}
        </p>
      )}
      {job?.notice && (
        <p className="alert" role="status">
          {job.notice}
        </p>
      )}
      {job && visible.length > 0 && (
        <p className="discovery-explanation">
          Оценка учитывает ваш словарь и диагностику. Она касается лексики;
          темп, акцент и понимание на слух проверяйте при просмотре.
        </p>
      )}
      <div className="discovery-grid">
        {visible.map((candidate) => (
          <article className="discovery-video" key={candidate.video_id}>
            <a
              className="discovery-thumbnail"
              href={`https://www.youtube.com/watch?v=${candidate.video_id}`}
              target="_blank"
              rel="noreferrer"
              aria-label={`Открыть ${candidate.title} на YouTube`}
            >
              <img
                src={`https://i.ytimg.com/vi/${candidate.video_id}/mqdefault.jpg`}
                alt=""
                loading="lazy"
              />
              {candidate.duration_seconds !== null && (
                <span>
                  {candidate.direct ? "≈ " : ""}
                  {time(candidate.duration_seconds * 1000)}
                </span>
              )}
            </a>
            <div className="discovery-video-body">
              <p className="discovery-channel">
                YouTube{candidate.channel ? ` · ${candidate.channel}` : ""}
              </p>
              <h2>{candidate.title}</h2>
              {candidate.status === "pending" ? (
                <p className="discovery-pending" role="status">
                  Получаем субтитры и оцениваем лексику…
                </p>
              ) : (
                <>
                  <p className="discovery-track">
                    {candidate.source === "youtube-authored"
                      ? "Авторские субтитры"
                      : "Автоматические субтитры"}
                    {candidate.cached ? " · из кэша" : ""}
                  </p>
                  {candidate.difficulty && (
                    <div className="discovery-coverage">
                      <div className="coverage-bar" aria-hidden="true">
                        <span
                          style={{
                            width: `${candidate.difficulty.known_percent ?? 0}%`,
                          }}
                        />
                        <i
                          style={{
                            width: `${candidate.difficulty.unfamiliar_percent ?? 0}%`,
                          }}
                        />
                      </div>
                      <p>
                        <strong>
                          {candidate.difficulty.known_percent?.toFixed(1) ??
                            "—"}
                          %
                        </strong>{" "}
                        знакомо по профилю
                        <br />
                        {candidate.difficulty.unassessed_percent?.toFixed(1) ??
                          "—"}
                        % пока не оценено
                      </p>
                      <strong>
                        {candidate.difficulty.label ??
                          "Недостаточно данных для оценки сложности"}
                      </strong>
                      {candidate.difficulty.provisional && (
                        <p className="help">
                          Предварительно · есть предположения по диагностике
                        </p>
                      )}
                      {candidate.difficulty.limitations.map((reason) => (
                        <p className="help" key={reason}>
                          {reason}
                        </p>
                      ))}
                    </div>
                  )}
                  {!!candidate.review_words?.length && (
                    <p className="discovery-review">
                      Встретятся изучаемые слова:{" "}
                      <strong>{candidate.review_words.join(", ")}</strong>
                    </p>
                  )}
                  <button
                    disabled={!!opening}
                    onClick={() => void openVideo(candidate)}
                  >
                    {opening === candidate.video_id
                      ? "Открываем…"
                      : "Смотреть с субтитрами"}
                  </button>
                </>
              )}
            </div>
          </article>
        ))}
      </div>
      {unavailable.length > 0 && (
        <details className="discovery-unavailable">
          <summary>Не удалось оценить: {unavailable.length}</summary>
          {unavailable.map((candidate) => (
            <p key={candidate.video_id}>
              <a
                href={`https://www.youtube.com/watch?v=${candidate.video_id}`}
                target="_blank"
                rel="noreferrer"
              >
                {candidate.title}
              </a>{" "}
              · {candidate.error}
            </p>
          ))}
        </details>
      )}
      <YouTubeSearchSettings onChanged={setSettings} />
    </section>
  );
}
