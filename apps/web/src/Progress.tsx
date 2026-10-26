import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { scores } from "./Study";
import "./progress.css";

type Day = {
  date: string;
  known: number | null;
  confident: number | null;
  known_change: number;
  baseline_change: number;
  watched_ms: number;
  active: boolean;
};
type Summary = {
  days: number;
  today: string;
  timezone: string;
  vocabulary_tracking_since: string;
  playback_tracking_since: string;
  counts: Record<string, number>;
  known_change: number;
  confident_change: number;
  watched_ms: number;
  active_days: number;
  streak: number;
  series: Day[];
  calendar: Array<Pick<Day, "date" | "watched_ms" | "active">>;
};
type DayDetails = {
  date: string;
  watched_ms: number;
  baseline_change: number;
  materials: Array<{ id: string; title: string; watched_ms: number }>;
  words: Array<{
    word: { lemma: string; part_of_speech: string };
    data: {
      previous: { score: number | null; ignored: boolean };
      current: { score: number | null; ignored: boolean };
    };
    material_id: string | null;
    material_title: string | null;
  }>;
  has_more: boolean;
};
const number = new Intl.NumberFormat("ru-RU");
const signed = (value: number) =>
  `${value > 0 ? "+" : ""}${number.format(value)}`;
const dateLabel = (
  value: string,
  options: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" },
) =>
  new Date(
    value.includes("T") ? value : `${value}T12:00:00`,
  ).toLocaleDateString("ru-RU", options);
function duration(ms: number) {
  if (!ms) return "0 мин";
  if (ms < 60_000) return `${Math.max(1, Math.round(ms / 1000))} сек`;
  const minutes = Math.floor(ms / 60_000);
  return minutes >= 60
    ? `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`
    : `${minutes} мин`;
}
function knowledge(value: { score: number | null; ignored: boolean }) {
  return value.ignored
    ? "Исключено"
    : value.score == null
      ? "Не оценено"
      : scores[value.score];
}

function VocabularyChart({ series }: { series: Day[] }) {
  const values = series.flatMap((day) => [day.known ?? 0, day.confident ?? 0]);
  const max = Math.max(4, ...values);
  const x = (index: number) =>
    50 + (index / Math.max(1, series.length - 1)) * 850;
  const y = (value: number) => 195 - (value / max) * 155;
  const path = (field: "known" | "confident") =>
    series
      .map((day, index) =>
        day[field] == null ? "" : `${x(index)},${y(day[field])}`,
      )
      .filter(Boolean)
      .join(" ");
  return (
    <>
      <svg
        className="vocabulary-chart"
        viewBox="0 0 930 235"
        role="img"
        aria-label="Размер знакомого словаря по дням"
      >
        <desc>
          Знакомые слова включают уровни 2 и 3. Уверенно известные — уровень 3.
          Дни до начала учёта не заполнены.
        </desc>
        {[0, 0.5, 1].map((part) => (
          <g key={part}>
            <line
              x1="50"
              x2="900"
              y1={y(max * part)}
              y2={y(max * part)}
              className="chart-gridline"
            />
            <text x="40" y={y(max * part) + 4} textAnchor="end">
              {number.format(Math.round(max * part))}
            </text>
          </g>
        ))}
        <polyline points={path("known")} className="known-line" />
        <polyline points={path("confident")} className="confident-line" />
        {series.filter((day) => day.known != null).length === 1 &&
          series.map(
            (day, index) =>
              day.known != null && (
                <circle
                  key={day.date}
                  cx={x(index)}
                  cy={y(day.known)}
                  r="4"
                  fill="var(--green)"
                />
              ),
          )}
        {[0, Math.floor((series.length - 1) / 2), series.length - 1].map(
          (index, label) => (
            <text
              key={label}
              x={x(index)}
              y="224"
              textAnchor={
                label === 0 ? "start" : label === 2 ? "end" : "middle"
              }
            >
              {dateLabel(series[index].date)}
            </text>
          ),
        )}
      </svg>
      <div className="progress-chart-legend">
        <span>
          <i />
          Знакомые · 2 и 3
        </span>
        <span>
          <i className="confident-dot" />
          Уверенно знаю · 3
        </span>
      </div>
    </>
  );
}

function Bars({
  series,
  field,
  onDay,
}: {
  series: Day[];
  field: "known_change" | "watched_ms";
  onDay(day: string): void;
}) {
  const max = Math.max(1, ...series.map((day) => Math.abs(day[field])));
  const signedScale = series.some((day) => day[field] < 0);
  return (
    <div className="progress-bars-scroll">
      <div
        className={`progress-bars${signedScale ? " signed" : ""}`}
        style={{
          gridTemplateColumns: `repeat(${series.length}, minmax(${series.length > 90 ? 4 : 9}px, 1fr))`,
        }}
      >
        {series.map((day) => {
          const label =
            field === "watched_ms"
              ? duration(day.watched_ms)
              : `${signed(day.known_change)} слов`;
          const baseline = day.baseline_change
            ? ` · диагностика и предварительные оценки: ${signed(day.baseline_change)}`
            : "";
          return (
            <button
              key={day.date}
              type="button"
              className={`progress-bar-day${day[field] < 0 ? " negative" : ""}`}
              title={`${dateLabel(day.date)} · ${label}${baseline}`}
              aria-label={`${dateLabel(day.date)}: ${label}${baseline}. Открыть день`}
              onClick={() => onDay(day.date)}
            >
              <span
                style={{
                  height: `${day[field] ? Math.max(3, (Math.abs(day[field]) / max) * (signedScale ? 48 : 100)) : 2}%`,
                }}
              />
            </button>
          );
        })}
      </div>
      <div className="bar-date-labels">
        <span>{dateLabel(series[0].date)}</span>
        <span>{dateLabel(series.at(-1)!.date)}</span>
      </div>
    </div>
  );
}

export function Progress({
  beforeLoad,
  onMaterial,
}: {
  beforeLoad(): Promise<void>;
  onMaterial(id: string): void;
}) {
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [details, setDetails] = useState<DayDetails | null>(null);
  const [dayError, setDayError] = useState("");
  const [dayLoading, setDayLoading] = useState(false);
  const [dayAttempt, setDayAttempt] = useState(0);
  const [moreLoading, setMoreLoading] = useState(false);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const beforeLoadRef = useRef(beforeLoad);
  beforeLoadRef.current = beforeLoad;
  const detailsPanel = useRef<HTMLElement>(null);
  const selection = useRef(selectedDay);
  selection.current = selectedDay;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    async function load() {
      try {
        await beforeLoadRef.current();
        const result = await api<Summary>(
          `/progress?days=${days}&timezone=${encodeURIComponent(zone)}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) setSummary(result);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [days, zone, attempt]);
  useEffect(() => {
    if (!selectedDay) return;
    const controller = new AbortController();
    setDayLoading(true);
    setDetails(null);
    setDayError("");
    setMoreLoading(false);
    void api<DayDetails>(
      `/progress/day?date=${selectedDay}&timezone=${encodeURIComponent(zone)}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) setDetails(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setDayError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDayLoading(false);
      });
    detailsPanel.current?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
    return () => controller.abort();
  }, [selectedDay, zone, dayAttempt]);
  async function more() {
    if (!details || moreLoading) return;
    const day = selectedDay;
    setMoreLoading(true);
    setDayError("");
    try {
      const result = await api<DayDetails>(
        `/progress/day?date=${day}&timezone=${encodeURIComponent(zone)}&offset=${details.words.length}`,
      );
      if (selection.current === day)
        setDetails(
          (current) =>
            current && {
              ...current,
              words: [...current.words, ...result.words],
              has_more: result.has_more,
            },
        );
    } catch (e) {
      if (selection.current === day) setDayError((e as Error).message);
    } finally {
      if (selection.current === day) setMoreLoading(false);
    }
  }
  return (
    <section className="progress-page">
      <div className="progress-heading">
        <div>
          <h1>Прогресс</h1>
        </div>
        <div
          className="progress-period"
          role="group"
          aria-label="Период статистики"
        >
          {[
            [7, "Неделя"],
            [30, "Месяц"],
            [90, "3 месяца"],
            [365, "Год"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={days === value ? "active" : ""}
              aria-pressed={days === value}
              onClick={() => setDays(Number(value))}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {error && (
        <p className="warning" role="alert">
          {error}{" "}
          <button
            className="quiet"
            onClick={() => setAttempt((value) => value + 1)}
          >
            Повторить
          </button>
        </p>
      )}
      {loading ? (
        <p className="progress-loading" role="status">
          Собираем статистику…
        </p>
      ) : (
        !error &&
        summary && (
          <>
            <div className="progress-metrics">
              <article>
                <span>Знакомые слова</span>
                <strong>{number.format(summary.counts.known)}</strong>
                <small>{signed(summary.known_change)} за период</small>
              </article>
              <article>
                <span>Уверенно знаю</span>
                <strong>{number.format(summary.counts.confident)}</strong>
                <small>{signed(summary.confident_change)} за период</small>
              </article>
              <article>
                <span>Время просмотра</span>
                <strong className="duration-metric">
                  {duration(summary.watched_ms)}
                </strong>
                <small>за выбранный период</small>
              </article>
              <article>
                <span>Дни занятий</span>
                <strong>
                  {summary.active_days}
                  <em> / {days}</em>
                </strong>
                <small>за выбранный период</small>
              </article>
            </div>
            {!summary.counts.known && !summary.active_days && (
              <p className="progress-empty">
                Откройте видео или добавьте знакомые слова — здесь появится ваш
                прогресс.
              </p>
            )}
            <section className="progress-card">
              <div className="progress-card-heading">
                <div>
                  <h2>Рост словаря</h2>
                  <p>Знакомые слова на конец каждого дня</p>
                </div>
                <span className="progress-badge">
                  {number.format(summary.counts.known)} сейчас
                </span>
              </div>
              <VocabularyChart series={summary.series} />
              <details className="progress-note">
                <summary>Как считается словарь</summary>
                <p>
                  История с {dateLabel(summary.vocabulary_tracking_since)}.
                  Словарь на эту дату — исходная точка. Учитываются сохранённые
                  оценки, включая автоматические и предварительные
                  {summary.counts.provisional
                    ? ` (${number.format(summary.counts.provisional)} знакомых слов)`
                    : ""}
                  .
                </p>
              </details>
            </section>
            <div className="progress-two-columns">
              <section className="progress-card">
                <div className="progress-card-heading">
                  <div>
                    <h2>Изменение словаря</h2>
                    <p>Прирост и снижение по дням</p>
                  </div>
                </div>
                <Bars
                  series={summary.series}
                  field="known_change"
                  onDay={setSelectedDay}
                />
              </section>
              <section className="progress-card">
                <div className="progress-card-heading">
                  <div>
                    <h2>Время просмотра</h2>
                    <p>Время со звуком</p>
                  </div>
                </div>
                <Bars
                  series={summary.series}
                  field="watched_ms"
                  onDay={setSelectedDay}
                />
                <details className="progress-note">
                  <summary>Как считается время</summary>
                  <p>
                    Учёт с {dateLabel(summary.playback_tracking_since)}.
                    Повторный просмотр учитывается; скорость воспроизведения не
                    увеличивает минуты.
                  </p>
                </details>
              </section>
            </div>
            <section className="progress-card">
              <div className="progress-card-heading">
                <div>
                  <h2>Ритм занятий</h2>
                  <p>Последние 365 дней · {zone}</p>
                </div>
                <span className="progress-badge">
                  {summary.streak}
                  {summary.streak === 365 ? "+" : ""} дн. подряд
                </span>
              </div>
              <div className="progress-calendar-scroll">
                <div className="calendar-months">
                  <span>
                    {dateLabel(summary.calendar[0].date, {
                      month: "long",
                      year: "numeric",
                    })}
                  </span>
                  <span>
                    {dateLabel(summary.today, {
                      month: "long",
                      year: "numeric",
                    })}
                  </span>
                </div>
                <div
                  className="progress-calendar"
                  style={{
                    gridTemplateColumns: `repeat(${Math.ceil((summary.calendar.length + ((new Date(summary.calendar[0].date + "T12:00:00").getDay() + 6) % 7)) / 7)}, 1fr)`,
                  }}
                >
                  {Array.from(
                    {
                      length:
                        (new Date(
                          summary.calendar[0].date + "T12:00:00",
                        ).getDay() +
                          6) %
                        7,
                    },
                    (_, i) => (
                      <span key={`pad-${i}`} />
                    ),
                  )}
                  {summary.calendar.map((day) => {
                    const intensity =
                      day.watched_ms >= 1_800_000
                        ? 4
                        : day.watched_ms >= 600_000
                          ? 3
                          : day.watched_ms > 0
                            ? 2
                            : day.active
                              ? 1
                              : 0;
                    const label = `${dateLabel(day.date, { day: "numeric", month: "long", year: "numeric" })}: ${duration(day.watched_ms)}${day.active ? ", есть занятия" : ", без занятий"}`;
                    return (
                      <button
                        type="button"
                        key={day.date}
                        className={`calendar-day intensity-${intensity}${selectedDay === day.date ? " selected" : ""}`}
                        title={label}
                        aria-label={label}
                        aria-pressed={selectedDay === day.date}
                        onClick={() => setSelectedDay(day.date)}
                      />
                    );
                  })}
                </div>
              </div>
              <div className="calendar-legend">
                <span>Без занятий</span>
                {[0, 1, 2, 3, 4].map((value) => (
                  <i key={value} className={`intensity-${value}`} />
                ))}
                <span>30+ минут</span>
              </div>
            </section>
            {selectedDay && (
              <section
                ref={detailsPanel}
                className="progress-card progress-day-details"
                aria-label="Выбранный день"
              >
                <div className="progress-card-heading">
                  <div>
                    <h2>
                      {dateLabel(selectedDay, {
                        day: "numeric",
                        month: "long",
                        year: "numeric",
                      })}
                    </h2>
                    <p>
                      {details
                        ? `${duration(details.watched_ms)} просмотра`
                        : "Занятия за день"}
                    </p>
                  </div>
                  <button
                    className="quiet"
                    aria-label="Закрыть день"
                    onClick={() => setSelectedDay(null)}
                  >
                    ×
                  </button>
                </div>
                {dayLoading && <p role="status">Загружаем день…</p>}
                {dayError && (
                  <p className="warning" role="alert">
                    {dayError}{" "}
                    <button
                      className="quiet"
                      onClick={() => setDayAttempt((value) => value + 1)}
                    >
                      Повторить
                    </button>
                  </p>
                )}
                {details && (
                  <>
                    {details.baseline_change !== 0 && (
                      <p className="progress-note">
                        Диагностика и предварительные оценки:{" "}
                        {signed(details.baseline_change)} знакомых слов.
                      </p>
                    )}
                    {details.materials.length > 0 && (
                      <div className="progress-materials">
                        {details.materials.map((material) => (
                          <button
                            type="button"
                            className="secondary"
                            key={material.id}
                            onClick={() => onMaterial(material.id)}
                          >
                            <span>{material.title}</span>
                            <small>{duration(material.watched_ms)} ↗</small>
                          </button>
                        ))}
                      </div>
                    )}
                    <h3>Изменения знакомости</h3>
                    {details.words.length ? (
                      <>
                        <ul className="progress-word-changes">
                          {details.words.map((entry, i) => (
                            <li key={i}>
                              <div>
                                <strong>{entry.word.lemma}</strong>
                                <small>{entry.word.part_of_speech}</small>
                              </div>
                              <span>
                                {knowledge(entry.data.previous)} →{" "}
                                {knowledge(entry.data.current)}
                              </span>
                              {entry.material_id && (
                                <button
                                  className="quiet"
                                  onClick={() => onMaterial(entry.material_id!)}
                                >
                                  {entry.material_title} ↗
                                </button>
                              )}
                            </li>
                          ))}
                        </ul>
                        {details.has_more && (
                          <button
                            className="secondary"
                            disabled={moreLoading}
                            onClick={() => void more()}
                          >
                            {moreLoading ? "Загружаем…" : "Показать ещё"}
                          </button>
                        )}
                      </>
                    ) : (
                      <p className="progress-note">
                        Изменений знакомости за этот день нет.
                      </p>
                    )}
                    {!details.materials.length && !details.words.length && (
                      <p className="progress-note">
                        Чтение переписки и другие действия могут отмечать день в
                        календаре. Время просмотра и изменения слов появляются
                        здесь, когда они сохранены.
                      </p>
                    )}
                  </>
                )}
              </section>
            )}
            <section className="progress-card">
              <div className="progress-card-heading">
                <div>
                  <h2>Состав словаря</h2>
                  <p>Базовые формы с учётом части речи</p>
                </div>
              </div>
              <div className="progress-levels">
                {scores.map((label, score) => (
                  <div key={score}>
                    <span>
                      {score} · {label}
                    </span>
                    <div className="level-track">
                      <i
                        className={`level-fill level-${score}`}
                        style={{
                          width: `${(summary.counts[String(score)] / Math.max(1, ...scores.map((_, i) => summary.counts[String(i)]))) * 100}%`,
                        }}
                      />
                    </div>
                    <strong>
                      {number.format(summary.counts[String(score)])}
                    </strong>
                  </div>
                ))}
              </div>
              <p className="progress-note">
                Пока не оценено: {number.format(summary.counts.unassessed)} ·
                исключено: {number.format(summary.counts.ignored)}. Исключённые
                слова не входят в знакомый словарь.
              </p>
            </section>
          </>
        )
      )}
    </section>
  );
}
