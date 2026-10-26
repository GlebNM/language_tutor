import { KnowledgeControl, LevelMark } from "./KnowledgeControl";
import { Fragment, useEffect, useRef, useState } from "react";
import { api, time } from "./api";
import {
  expressionHighlights,
  orderedTokens,
  phrasalForToken,
  sourceKey,
} from "./expressionSelection";
import type {
  ExpressionEntry,
  ExpressionKind,
  ExpressionSource,
  ExpressionState,
} from "./expressionSelection";
import "./expressions.css";
import { routeHash } from "./navigation";

const kinds: Record<ExpressionKind, string> = {
  phrasal_verb: "Фразовый глагол",
  idiom: "Идиома",
  collocation: "Устойчивое сочетание",
};
const levels = ["Не знаю", "Учу", "Узнаю в контексте", "Уверенно знаю"];
const empty: ExpressionState = {
  available: false,
  occurrences: [],
  job: null,
  counts: { total: 0, unassessed: 0, learning: 0 },
};

export function useExpressions(source: ExpressionSource | null) {
  const key = sourceKey(source);
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const generation = useRef(0);
  const focusVersion = useRef(0);
  const dataEpoch = useRef(0);
  const [stored, setStored] = useState<{ key: string; data: ExpressionState }>({
    key: "",
    data: empty,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeToken, setActiveToken] = useState<string | null>(null);
  const [explicitId, setExplicitId] = useState<string | null>(null);
  const [manualMode, setManualMode] = useState(false);
  const [manualIds, setManualIds] = useState<string[]>([]);
  const [attempt, setAttempt] = useState(0);
  const data = stored.key === key ? stored.data : empty;

  useEffect(() => {
    const selected = sourceRef.current;
    const version = ++generation.current;
    ++focusVersion.current;
    setActiveToken(null);
    setExplicitId(null);
    setManualMode(false);
    setManualIds([]);
    setError("");
    setBusy(false);
    if (!selected) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let started = false;
    async function poll() {
      const epoch = dataEpoch.current;
      try {
        const params = new URLSearchParams(selected as Record<string, string>);
        let value = await api<ExpressionState>(
          `/expressions/source?${params}`,
          { signal: controller.signal },
        );
        if (!value.job && value.available && !started) {
          started = true;
          value = await api<ExpressionState>("/expressions/analyze", {
            method: "POST",
            signal: controller.signal,
            body: JSON.stringify(selected),
          });
        }
        if (generation.current !== version || controller.signal.aborted) return;
        if (epoch === dataEpoch.current) setStored({ key, data: value });
        if (value.job?.status === "queued" || value.job?.status === "running")
          timer = window.setTimeout(() => void poll(), 1800);
      } catch (e) {
        if (!controller.signal.aborted && generation.current === version)
          setError((e as Error).message);
      }
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      ++generation.current;
    };
  }, [key, attempt]);

  async function reload() {
    const selected = sourceRef.current;
    const version = generation.current;
    const epoch = ++dataEpoch.current;
    if (!selected) return;
    try {
      const params = new URLSearchParams(selected as Record<string, string>);
      const result = await api<ExpressionState>(
        `/expressions/source?${params}`,
      );
      if (version === generation.current && epoch === dataEpoch.current)
        setStored({ key, data: result });
    } catch (e) {
      if (version === generation.current) setError((e as Error).message);
    }
  }

  async function resolve(ids: string[], manual = false) {
    const selected = sourceRef.current;
    if (!selected) return;
    const version = generation.current;
    const focus = ++focusVersion.current;
    setError("");
    if (
      !manual &&
      (phrasalForToken(data.occurrences, ids[0]).length ||
        data.job?.status === "completed")
    ) {
      setBusy(false);
      return;
    }
    setBusy(true);
    ++dataEpoch.current;
    try {
      const result = await api<ExpressionState>("/expressions/selection", {
        method: "POST",
        body: JSON.stringify({ ...selected, token_ids: ids, manual }),
      });
      if (version !== generation.current || focus !== focusVersion.current)
        return;
      ++dataEpoch.current;
      setStored({ key, data: result });
      if (manual) {
        const chosen = result.occurrences.find(
          (entry) =>
            ids[0] &&
            entry.token_ids?.includes(ids[0]) &&
            entry.token_ids.includes(ids[ids.length - 1]),
        );
        if (chosen) {
          setExplicitId(chosen.occurrence_id ?? null);
          setManualMode(false);
          setManualIds([]);
        } else
          setError(
            "Подходящее выражение не найдено. Уточните выбранные слова и повторите разбор.",
          );
      }
    } catch (e) {
      if (version === generation.current && focus === focusVersion.current)
        setError((e as Error).message);
    } finally {
      if (version === generation.current && focus === focusVersion.current)
        setBusy(false);
    }
  }

  function focus(id: string) {
    setActiveToken(id);
    setExplicitId(null);
    void resolve([id]);
  }
  function intercept(id: string): boolean {
    if (!manualMode) return false;
    setManualIds((ids) =>
      ids.includes(id)
        ? ids.filter((value) => value !== id)
        : ids.length < 24
          ? [...ids, id]
          : ids,
    );
    return true;
  }
  function clearFocus() {
    ++focusVersion.current;
    setActiveToken(null);
    setExplicitId(null);
    setBusy(false);
  }
  const phrasal = phrasalForToken(data.occurrences, activeToken);
  const explicit = data.occurrences.find(
    (entry) => entry.occurrence_id === explicitId,
  );
  const highlighted = expressionHighlights(
    data.occurrences,
    activeToken,
    explicitId,
  );
  for (const id of manualIds) highlighted.add(id);
  return {
    data,
    error,
    busy,
    activeToken,
    manualMode,
    manualIds,
    highlighted,
    primary: explicit ?? phrasal[0] ?? null,
    otherMatches: data.occurrences.filter(
      (entry) =>
        entry.kind !== "phrasal_verb" &&
        activeToken &&
        entry.token_ids?.includes(activeToken) &&
        entry.occurrence_id !== explicitId,
    ),
    focus,
    intercept,
    clearFocus,
    reload,
    choose(entry: ExpressionEntry) {
      setExplicitId(entry.occurrence_id ?? null);
    },
    toggleManual() {
      clearFocus();
      setManualMode((value) => !value);
      setManualIds([]);
    },
    analyzeManual(order: string[]) {
      void resolve(orderedTokens(manualIds, order), true);
    },
    async retry() {
      const selected = sourceRef.current;
      if (!selected) return;
      const version = generation.current;
      setError("");
      try {
        await api("/expressions/analyze", {
          method: "POST",
          body: JSON.stringify(selected),
        });
        if (version === generation.current) setAttempt((value) => value + 1);
      } catch (e) {
        if (version === generation.current) setError((e as Error).message);
      }
    },
  };
}

export type ExpressionsView = ReturnType<typeof useExpressions>;

export function ExpressionToolbar({
  view,
  order,
  onBegin,
}: {
  view: ExpressionsView;
  order: string[];
  onBegin?(): void;
}) {
  return (
    <div className="expression-toolbar">
      <button
        type="button"
        className="quiet"
        aria-pressed={view.manualMode}
        onClick={() => {
          if (!view.manualMode) onBegin?.();
          view.toggleManual();
        }}
      >
        {view.manualMode ? "Отменить выделение" : "Выделить выражение"}
      </button>
      {view.manualMode && (
        <>
          <span>Нажмите на слова выражения · {view.manualIds.length}/24</span>
          <button
            type="button"
            className="secondary"
            disabled={view.manualIds.length < 2 || view.busy}
            onClick={() => view.analyzeManual(order)}
          >
            {view.busy ? "Разбираем…" : "Разобрать выбранное"}
          </button>
        </>
      )}
    </div>
  );
}

function ExpressionKnowledgeEditor({
  entry,
  onSaved,
}: {
  entry: ExpressionEntry;
  onSaved(): void;
}) {
  return (
    <KnowledgeControl
      identity={entry.id}
      label="Знакомость выражения"
      value={entry.knowledge}
      unsaved={!entry.saved}
      onChange={async (value) => {
        await api(`/expressions/${entry.id}/knowledge`, {
          method: "PUT",
          body: JSON.stringify(value),
        });
        onSaved();
      }}
    />
  );
}

export function ExpressionCard({
  entry,
  onSaved,
  onDismiss,
  onSeek,
}: {
  entry: ExpressionEntry;
  onSaved(): void;
  onDismiss?(): void;
  onSeek?(ms: number): void;
}) {
  return (
    <article className="expression-card" data-expression-kind={entry.kind}>
      <p className="eyebrow">{kinds[entry.kind]}</p>
      <h3>{entry.canonical}</h3>
      <p className="expression-translation">{entry.translation}</p>
      <ExpressionKnowledgeEditor entry={entry} onSaved={onSaved} />
      <p>{entry.explanation}</p>
      {entry.contexts?.map((context) => (
        <blockquote key={context.cue_id}>
          {context.anchors?.length
            ? (() => {
                const chars = Array.from(context.text);
                let end = 0;
                return (
                  <>
                    {context.anchors.map((anchor, i) => {
                      const before = chars
                        .slice(end, anchor.start_offset)
                        .join("");
                      end = anchor.end_offset;
                      return (
                        <Fragment key={i}>
                          {before}
                          <mark className="expression-component">
                            {chars
                              .slice(anchor.start_offset, anchor.end_offset)
                              .join("")}
                          </mark>
                        </Fragment>
                      );
                    })}
                    {chars.slice(end).join("")}
                  </>
                );
              })()
            : context.text}
          {typeof context.start_ms === "number" && onSeek && (
            <button
              className="timestamp"
              onClick={() => onSeek(context.start_ms!)}
            >
              Послушать · {time(context.start_ms)}
            </button>
          )}
        </blockquote>
      ))}
      <details className="expression-usage">
        <summary>Пример и употребление</summary>
        <p>{entry.usage}</p>
        <p className="example">{entry.example}</p>
      </details>
      {onDismiss && (
        <button
          type="button"
          className="quiet expression-dismiss"
          onClick={onDismiss}
        >
          Здесь выделено ошибочно
        </button>
      )}
    </article>
  );
}

export function ExpressionInspector({
  view,
  onSeek,
}: {
  view: ExpressionsView;
  onSeek?(ms: number): void;
}) {
  async function dismiss(entry: ExpressionEntry) {
    if (!entry.occurrence_id) return;
    await api(`/expressions/occurrences/${entry.occurrence_id}/dismiss`, {
      method: "POST",
      body: "{}",
    });
    await view.reload();
  }
  const [error, setError] = useState("");
  const card = (entry: ExpressionEntry) => (
    <ExpressionCard
      key={entry.occurrence_id}
      entry={entry}
      onSeek={onSeek}
      onSaved={() => void view.reload()}
      onDismiss={() =>
        void dismiss(entry).catch((e) => setError((e as Error).message))
      }
    />
  );
  return (
    <div className="expression-inspector">
      {view.busy && (
        <p role="status">Проверяем выражения в выбранной реплике…</p>
      )}
      {view.error && (
        <p className="warning" role="alert">
          {view.error}
        </p>
      )}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {view.primary && card(view.primary)}
      {view.otherMatches.length > 0 && (
        <details className="other-expressions">
          <summary>
            Идиомы и устойчивые сочетания · {view.otherMatches.length}
          </summary>
          {view.otherMatches.map((entry) => (
            <div key={entry.occurrence_id}>
              <button className="quiet" onClick={() => view.choose(entry)}>
                Выделить {entry.canonical}
              </button>
              {card(entry)}
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

export function ExpressionList({
  view,
  onOpen,
}: {
  view: ExpressionsView;
  onOpen(entry: ExpressionEntry): void;
}) {
  const phrasal = view.data.occurrences.filter(
    (entry) => entry.kind === "phrasal_verb",
  );
  const others = view.data.occurrences.filter(
    (entry) => entry.kind !== "phrasal_verb",
  );
  const rows = (entries: ExpressionEntry[]) => (
    <div className="expression-list">
      {entries.map((entry) => (
        <button
          type="button"
          className="expression-item"
          key={entry.occurrence_id}
          onClick={() => onOpen(entry)}
        >
          <strong>{entry.canonical}</strong>
          <span>{entry.translation}</span>
          <span className="expression-level">
            <LevelMark score={entry.knowledge.score} />
            {entry.knowledge.ignored ? "Исключено" : entry.knowledge.status}
          </span>
        </button>
      ))}
    </div>
  );
  return (
    <section className="material-expressions" aria-label="Выражения материала">
      <h2>Выражения видео</h2>
      {(view.data.job?.status === "queued" ||
        view.data.job?.status === "running") && (
        <p role="status">
          Ищем выражения · {view.data.job.completed_batches} /{" "}
          {view.data.job.total_batches}
        </p>
      )}
      {view.data.job?.status === "failed" && (
        <p className="warning">{view.data.job.error}</p>
      )}
      {(view.data.job?.status === "failed" ||
        (!view.data.job && view.data.available)) && (
        <button className="quiet" onClick={() => void view.retry()}>
          Повторить поиск выражений
        </button>
      )}
      {!view.data.available && !view.data.job && (
        <p className="help">
          Для поиска выражений подключите ChatGPT в настройках.
        </p>
      )}
      {view.error && (
        <p className="warning" role="alert">
          {view.error}
        </p>
      )}
      {phrasal.length > 0 && rows(phrasal)}
      {view.data.job?.status === "completed" && !phrasal.length && (
        <p className="help">Фразовые глаголы не найдены.</p>
      )}
      <details className="other-expressions">
        <summary>Идиомы и устойчивые сочетания · {others.length}</summary>
        {rows(others)}
      </details>
      {view.data.counts.total > 0 && (
        <p className="help">
          Найдено вхождений: {view.data.counts.total} · пока не оценено:{" "}
          {view.data.counts.unassessed} · учу: {view.data.counts.learning}
        </p>
      )}
    </section>
  );
}

export function ExpressionDictionary({
  refreshKey = 0,
}: {
  refreshKey?: number;
}) {
  const [entries, setEntries] = useState<ExpressionEntry[]>([]);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [score, setScore] = useState("");
  const [selected, setSelected] = useState<ExpressionEntry | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const detailVersion = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    const params = new URLSearchParams({ search, kind, score });
    api<ExpressionEntry[]>(`/expressions?${params}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) {
          setEntries(value);
          setError("");
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [search, kind, score, revision, refreshKey]);
  useEffect(
    () => () => {
      ++detailVersion.current;
    },
    [],
  );
  async function open(entry: ExpressionEntry) {
    const version = ++detailVersion.current;
    setSelected(null);
    try {
      const result = await api<ExpressionEntry>(`/expressions/${entry.id}`);
      if (version === detailVersion.current) setSelected(result);
    } catch (e) {
      if (version === detailVersion.current) setError((e as Error).message);
    }
  }
  return (
    <section className="expression-dictionary">
      <div className="dictionary-filters">
        <label>
          Поиск выражения
          <input value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label>
          Тип
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">Все выражения</option>
            {Object.entries(kinds).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Знакомость
          <select value={score} onChange={(e) => setScore(e.target.value)}>
            <option value="">Все оценки</option>
            <option value="unassessed">Пока не оценено</option>
            {levels.map((level, i) => (
              <option value={i} key={i}>
                {level}
              </option>
            ))}
          </select>
        </label>
      </div>
      {busy && <p role="status">Загружаем выражения…</p>}
      {error && (
        <p className="warning" role="alert">
          {error}
          <button
            className="quiet"
            onClick={() => setRevision((value) => value + 1)}
          >
            Повторить
          </button>
        </p>
      )}
      {!busy && !entries.length && (
        <p>
          Пока нет подходящих выражений. Откройте видео и сохраните выражение из
          его карточки.
        </p>
      )}
      <div className="expression-list">
        {entries.map((entry) => (
          <button
            className="expression-item"
            key={entry.id}
            onClick={() => void open(entry)}
          >
            <strong>{entry.canonical}</strong>
            <span>{entry.translation}</span>
            <small>
              {kinds[entry.kind]} · {entry.knowledge.status}
            </small>
          </button>
        ))}
      </div>
      {selected && (
        <div className="expression-dictionary-detail">
          <button
            className="quiet"
            onClick={() => {
              ++detailVersion.current;
              setSelected(null);
            }}
          >
            Закрыть выражение
          </button>
          <ExpressionCard
            entry={selected}
            onSaved={() => {
              setRevision((value) => value + 1);
              void open(selected);
            }}
          />
          <h3>Ваши примеры</h3>
          {selected.examples?.map((example) => (
            <blockquote key={example.id}>
              {example.contexts.map((context) => (
                <p key={context.cue_id}>
                  {context.text}
                  {context.material_id && !context.youtube_video_id && <a href={routeHash({section: "materials", materialId: context.material_id, blockId: context.block_id ?? context.cue_id})}>{context.title ?? "Открыть текст"} · к абзацу ↗</a>}
                  {context.youtube_video_id && (
                    <a
                      href={`https://www.youtube.com/watch?v=${context.youtube_video_id}&t=${Math.floor((context.start_ms ?? 0) / 1000)}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {context.title} · {time(context.start_ms ?? 0)} ↗
                    </a>
                  )}
                </p>
              ))}
            </blockquote>
          ))}
        </div>
      )}
    </section>
  );
}
