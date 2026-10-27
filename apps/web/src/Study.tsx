import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, time } from "./api";
import type { Citation, Difficulty, Knowledge, Selection, Token } from "./api";
import { MaterialCitation } from "./MaterialCitation";
import type { Cue } from "./api";
import { filterVideoVocabulary, groupVideoVocabulary } from "./videoVocabulary";
import { ExpressionDictionary } from "./Expressions";
import {
  KnowledgeControl,
  knowledgeNames,
  LevelMark,
} from "./KnowledgeControl";
import { Tabs } from "./UI";

export const scores = knowledgeNames;
export function knowledgeLabel(knowledge?: Knowledge) {
  if (knowledge?.ignored) return "Исключено из оценки";
  return knowledge?.score == null ? "Пока не оценено" : scores[knowledge.score];
}
function aiProvenance(value?: {
  model?: string | null;
  reasoning_effort?: string | null;
}) {
  if (!value?.model) return null;
  const reasoning = Object.prototype.hasOwnProperty.call(
    value,
    "reasoning_effort",
  )
    ? (value.reasoning_effort ?? "по умолчанию")
    : "не записан";
  return (
    <details className="ai-provenance">
      <summary>Об ответе</summary>
      <p>
        {value.model} · {reasoning}
      </p>
    </details>
  );
}

export function FamiliarityEditor({
  token,
  onSaved,
}: {
  token: Omit<Token, "material_id" | "cue_id">;
  onSaved(): void;
}) {
  return (
    <KnowledgeControl
      identity={token.lexeme_id ?? token.id}
      value={{
        score: token.knowledge?.score ?? null,
        ignored: token.knowledge?.ignored ?? false,
      }}
      provisional={token.knowledge?.provisional}
      unsaved={token.vocabulary?.eligible === false}
      onChange={async (value) => {
        await api(`/lexemes/${token.lexeme_id}/knowledge`, {
          method: "PUT",
          body: JSON.stringify(value),
        });
        onSaved();
      }}
    />
  );
}

export function DifficultyPanel({ value }: { value?: Difficulty }) {
  if (!value) return null;
  const number = (key: string) =>
    typeof value[key] === "number" ? Number(value[key]) : null;
  const familiar = number("known_percent");
  const unfamiliar = number("unfamiliar_percent");
  const unassessed = number("unassessed_percent");
  return (
    <details className="difficulty-card">
      <summary>
        <span>
          {familiar != null ? (
            <>
              <strong>{familiar.toFixed(0)}%</strong> знакомых слов
            </>
          ) : (
            "Сложность: мало данных"
          )}
        </span>
        {value.provisional === true && (
          <span className="badge">Предварительно</span>
        )}
        <span className="difficulty-summary">
          {typeof value.label === "string" ? value.label : "Подробнее"}
        </span>
      </summary>
      {familiar != null && (
        <>
          <div className="coverage-bar" aria-hidden="true">
            <span style={{ width: `${familiar}%` }} />
            <i style={{ width: `${unfamiliar ?? 0}%` }} />
          </div>
          <p>
            {familiar.toFixed(1)}% знакомо · {unfamiliar?.toFixed(1)}% незнакомо
            · {unassessed?.toFixed(1)}% без оценки
          </p>
        </>
      )}
      {typeof value.unique_unfamiliar === "number" && (
        <p>Незнакомых слов: {value.unique_unfamiliar}</p>
      )}
      {Array.isArray(value.coverage_interval) && (
        <p>
          С учётом неоценённых слов:{" "}
          {Number(value.coverage_interval[0]).toFixed(1)}–
          {Number(value.coverage_interval[1]).toFixed(1)}%.
        </p>
      )}
      {Number(value.provisional_percent) > 0 && (
        <p>
          Предварительно оценено {Number(value.provisional_percent).toFixed(1)}%
          вхождений; {Number(value.provisional_known_percent).toFixed(1)} п. п.
          знакомой лексики основаны на предположении.
        </p>
      )}
      {Array.isArray(value.limitations) &&
        value.limitations.map((limitation, index) => (
          <p key={index}>{String(limitation)}</p>
        ))}
    </details>
  );
}

export function VideoVocabulary({
  cues,
  isComplete,
  onSaved,
  onOccurrence,
  title = "Слова видео",
}: {
  cues: Cue[];
  isComplete: boolean;
  onSaved(): void;
  onOccurrence(cue: Cue, token: Token): void;
  title?: string;
}) {
  const entries = useMemo(() => groupVideoVocabulary(cues), [cues]);
  const [search, setSearch] = useState("");
  const [score, setScore] = useState("");
  const [ignored, setIgnored] = useState("");
  const [selectedLexeme, setSelectedLexeme] = useState<string | null>(null);
  const visible = filterVideoVocabulary(entries, search, score, ignored);
  const levels: Array<{
    score: number | null;
    label: string;
    marker: string;
    className: string;
  }> = [
    { score: null, label: "Пока не оценено", marker: "—", className: "null" },
    { score: 0, label: scores[0], marker: "0", className: "0" },
    { score: 1, label: scores[1], marker: "1", className: "1" },
    { score: 2, label: scores[2], marker: "2", className: "2" },
    { score: 3, label: scores[3], marker: "3", className: "3" },
  ].filter(
    (level) =>
      !score ||
      (score === "unassessed"
        ? level.score == null
        : String(level.score) === score),
  );
  return (
    <section
      className="video-vocabulary"
      aria-labelledby="video-vocabulary-title"
    >
      <div className="panel-title">
        <div>
          <h2 id="video-vocabulary-title">{title}</h2>
        </div>
        <span>
          {visible.length} из {entries.length}
        </span>
      </div>
      {!isComplete && (
        <p className="help">
          Список содержит только загруженную часть субтитров. Для оценки всего
          видео нужна полная дорожка.
        </p>
      )}
      <div className="dictionary-filters video-vocabulary-filters">
        <label>
          Поиск
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Базовая форма или словоформа"
          />
        </label>
        <label>
          Знакомость
          <select
            value={score}
            onChange={(event) => setScore(event.target.value)}
          >
            <option value="">Все оценки</option>
            <option value="unassessed">Пока не оценено</option>
            {scores.map((label, value) => (
              <option key={value} value={value}>
                {value} · {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Исключённые
          <select
            value={ignored}
            onChange={(event) => setIgnored(event.target.value)}
          >
            <option value="">Все слова</option>
            <option value="false">Учитываемые</option>
            <option value="true">Исключённые</option>
          </select>
        </label>
      </div>
      {!visible.length ? (
        <p className="help">Нет слов по выбранным условиям.</p>
      ) : (
        <div className="video-vocabulary-groups">
          {levels.map((level) => {
            const levelEntries = visible.filter(
              (entry) => entry.knowledge.score === level.score,
            );
            if (!levelEntries.length) return null;
            return (
              <section
                className="video-vocabulary-group"
                key={level.className}
                aria-labelledby={`video-vocabulary-level-${level.className}`}
              >
                <h3 id={`video-vocabulary-level-${level.className}`}>
                  <LevelMark score={level.score} />
                  {level.label}
                  <small>{levelEntries.length}</small>
                </h3>
                <div className="video-word-list">
                  {levelEntries.map((entry) => {
                    const representative = entry.occurrences[0]?.token;
                    if (!representative) return null;
                    const key =
                      entry.lexeme_id ||
                      `${entry.lemma}-${entry.part_of_speech}`;
                    const editable = {
                      ...representative,
                      lexeme_id: entry.lexeme_id || representative.lexeme_id,
                      lemma: entry.lemma,
                      part_of_speech: entry.part_of_speech,
                      knowledge: entry.knowledge,
                    };
                    const expanded = selectedLexeme === key;
                    return (
                      <Fragment key={key}>
                        <button
                          type="button"
                          className={`video-word-chip score-${level.className}${entry.knowledge.ignored ? " ignored" : ""}${expanded ? " selected" : ""}`}
                          title={`${level.marker} · ${level.label}${entry.knowledge.ignored ? " · Исключено" : ""}`}
                          aria-expanded={expanded}
                          onClick={() =>
                            setSelectedLexeme(expanded ? null : key)
                          }
                        >
                          <strong>{entry.lemma}</strong>
                          <small>{entry.part_of_speech}</small>
                          <span className="chip-count">×{entry.count}</span>
                          <LevelMark score={entry.knowledge.score} />
                          {entry.knowledge.ignored && (
                            <span className="chip-score">Исключено</span>
                          )}
                          {entry.knowledge.provisional && (
                            <span className="chip-provisional">
                              Предварительно
                            </span>
                          )}
                        </button>
                        {expanded && (
                          <div
                            className="video-word-details"
                            key={`details-${key}`}
                          >
                            <div className="video-word-summary">
                              <div>
                                <h4>
                                  {entry.lemma}{" "}
                                  <small>{entry.part_of_speech}</small>
                                </h4>
                                <p>
                                  Формы: {entry.forms.join(", ")} ·{" "}
                                  {entry.count}{" "}
                                  {entry.count === 1
                                    ? "вхождение"
                                    : "вхождений"}
                                </p>
                              </div>
                              <button
                                type="button"
                                className="quiet"
                                onClick={() => setSelectedLexeme(null)}
                                aria-label="Закрыть слово"
                              >
                                ×
                              </button>
                            </div>
                            <FamiliarityEditor
                              token={editable}
                              onSaved={onSaved}
                            />
                            <VideoOccurrences
                              occurrences={entry.occurrences}
                              onOccurrence={onOccurrence}
                            />
                          </div>
                        )}
                      </Fragment>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}

function VideoOccurrences({
  occurrences,
  onOccurrence,
}: {
  occurrences: Array<{ cue: Cue; token: Token }>;
  onOccurrence(cue: Cue, token: Token): void;
}) {
  const [limit, setLimit] = useState(50);
  return (
    <details className="video-word-occurrences">
      <summary>Показать вхождения ({occurrences.length})</summary>
      <ol>
        {occurrences.slice(0, limit).map(({ cue, token }) => (
          <li key={token.id}>
            <button
              className="timestamp"
              onClick={() => onOccurrence(cue, token)}
            >
              {typeof cue.start_ms === "number" ? time(cue.start_ms) : "К абзацу"}
            </button>
            <span>{cue.text}</span>
            <span className="occurrence-form">{token.surface}</span>
          </li>
        ))}
      </ol>
      {limit < occurrences.length && (
        <button
          className="quiet"
          onClick={() => setLimit((value) => value + 50)}
        >
          Показать ещё вхождения
        </button>
      )}
    </details>
  );
}

type AIModelOption = {
  id: string;
  name: string;
  reasoning_efforts: string[];
  default_reasoning_effort: string | null;
};
type AISettingsValue = {
  model: string;
  reasoning_effort: string | null;
  temporary_model: boolean;
  temporary_model_available: boolean;
};
export function AIModelSettings() {
  const [models, setModels] = useState<AIModelOption[]>([]);
  const [model, setModel] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [temporaryModel, setTemporaryModel] = useState(false);
  const [temporaryModelAvailable, setTemporaryModelAvailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setModels([]);
    setModel("");
    setReasoning("");
    setMessage("");
    async function load() {
      try {
        const saved = await api<AISettingsValue>("/ai/settings", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setModel(saved.model);
        setReasoning(saved.reasoning_effort ?? "");
        setTemporaryModel(saved.temporary_model);
        setTemporaryModelAvailable(saved.temporary_model_available);
        if (saved.temporary_model) return;
        const catalogue = await api<{ models: AIModelOption[] }>("/ai/models", {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) setModels(catalogue.models);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [loadAttempt]);
  const selected = models.find((option) => option.id === model);
  async function toggleTemporaryModel(enabled: boolean) {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const saved = await api<AISettingsValue>("/ai/temporary-model", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      });
      setTemporaryModel(saved.temporary_model);
      setLoadAttempt((value) => value + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!model) return;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const saved = await api<AISettingsValue>("/ai/settings", {
        method: "PUT",
        body: JSON.stringify({ model, reasoning_effort: reasoning || null }),
      });
      setModel(saved.model);
      setReasoning(saved.reasoning_effort ?? "");
      setMessage("Настройки сохранены.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="settings-card ai-model-settings">
      <h2>Учебный AI</h2>
      <label className="temporary-model-toggle">
        <input
          type="checkbox"
          role="switch"
          checked={temporaryModel}
          disabled={loading || saving || (!temporaryModelAvailable && !temporaryModel)}
          onChange={(event) => void toggleTemporaryModel(event.target.checked)}
        />
        Временная модель · gemma4:31b (Ollama Cloud)
      </label>
      {!loading && !temporaryModelAvailable && (
        <p>Для временной модели добавьте OLLAMA_API_KEY в .env и перезапустите приложение.</p>
      )}

      {loading ? (
        <p role="status">Загружаем доступные модели…</p>
      ) : temporaryModel ? (
        <>
          <p role="status">Новые запросы используют gemma4:31b через Ollama Cloud.</p>
          <p>Выключите временную модель, чтобы вернуться к выбранной модели ChatGPT.</p>
          {error && <p role="alert" className="warning">{error}</p>}
        </>
      ) : (
        <form onSubmit={(event) => void save(event)}>
          <label>
            Модель
            <select
              value={model}
              disabled={saving || !models.length}
              onChange={(event) => {
                const next = event.target.value;
                const option = models.find((item) => item.id === next);
                setModel(next);
                setReasoning(option?.default_reasoning_effort ?? "");
                setMessage("");
              }}
            >
              {!selected && model && (
                <option value={model} disabled>
                  {model} ·{" "}
                  {models.length ? "сейчас недоступна" : "сохранённая модель"}
                </option>
              )}
              {models.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Глубина рассуждения
            <select
              value={reasoning}
              disabled={!selected || saving}
              onChange={(event) => {
                setReasoning(event.target.value);
                setMessage("");
              }}
            >
              <option value="">По умолчанию для модели</option>
              {reasoning &&
                !selected?.reasoning_efforts.includes(reasoning) && (
                  <option value={reasoning} disabled>
                    {reasoning} ·{" "}
                    {selected ? "сейчас недоступен" : "сохранённый уровень"}
                  </option>
                )}
              {selected?.reasoning_efforts.map((effort) => (
                <option key={effort} value={effort}>
                  {effort}
                </option>
              ))}
            </select>
          </label>
          {error && (
            <p role="alert" className="warning">
              {error}
            </p>
          )}
          {message && <p role="status">{message}</p>}
          <div className="actions">
            <button disabled={saving || loading || !model || !selected}>
              {saving ? "Сохраняем…" : "Сохранить"}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={loading || saving}
              onClick={() => setLoadAttempt((value) => value + 1)}
            >
              Обновить каталог
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

type CardResult = {
  card: {
    translation: string;
    explanation: string;
    expression: string;
    example: string;
  };
  duration_ms: number;
  model?: string;
  reasoning_effort?: string | null;
};
type ChatAnswer = {
  answer: string;
  educational_example?: string | null;
  citations?: Citation[];
  model?: string;
  reasoning_effort?: string | null;
};
type Turn = {
  request_id: string;
  message: string;
  response: ChatAnswer | null;
};
type Conversation = { id: string; token_id?: string; messages: Turn[] };

export function LearningPanel({
  selection,
  onSaved,
  onSeek,
}: {
  selection: Selection;
  onSaved(): void;
  onSeek(milliseconds: number): void;
}) {
  const [card, setCard] = useState<CardResult | null>(null);
  const [cardError, setCardError] = useState("");
  const [cardBusy, setCardBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [forceRefreshCount, setForceRefreshCount] = useState(0);
  const [chat, setChat] = useState<Conversation | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [chatError, setChatError] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatHistoryLoading, setChatHistoryLoading] = useState(true);
  const [chatHistoryError, setChatHistoryError] = useState("");
  const [chatHistoryAttempt, setChatHistoryAttempt] = useState(0);
  const retry = useRef<{ message: string; request_id: string } | null>(null);
  const generation = useRef(0);
  const [lemma, setLemma] = useState(selection.token.lemma);
  const [pos, setPos] = useState(selection.token.part_of_speech);
  const [correctionError, setCorrectionError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setCard(null);
    setCardBusy(true);
    setCardError("");
    api<CardResult>("/cards", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        material_id: selection.material_id,
        cue_id: selection.cue.id,
        token_id: selection.token.id,
        force_refresh: forceRefreshCount > 0,
      }),
    })
      .then(setCard)
      .catch((e) => {
        if (e.name !== "AbortError") setCardError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setCardBusy(false);
      });
    return () => controller.abort();
  }, [
    selection.material_id,
    selection.cue.id,
    selection.token.id,
    selection.token.lexeme_id,
    attempt,
    forceRefreshCount,
  ]);
  useEffect(() => {
    ++generation.current;
    setChat(null);
    setChatOpen(false);
    setChatError("");
    setChatBusy(false);
    retry.current = null;
    return () => {
      ++generation.current;
    };
  }, [selection.material_id, selection.cue.id, selection.token.id]);
  useEffect(() => {
    setLemma(selection.token.lemma);
    setPos(selection.token.part_of_speech);
    setCorrectionError("");
  }, [
    selection.token.id,
    selection.token.lemma,
    selection.token.part_of_speech,
  ]);
  useEffect(() => {
    const controller = new AbortController();
    setChatHistoryLoading(true);
    setChatHistoryError("");
    api<Conversation[]>(`/conversations?material_id=${selection.material_id}`, {
      signal: controller.signal,
    })
      .then(async (conversations) => {
        const previous = conversations.find(
          (item) => item.token_id === selection.token.id,
        );
        if (previous) {
          const saved = await api<Conversation>(
            `/conversations/${previous.id}`,
            { signal: controller.signal },
          );
          if (!controller.signal.aborted) setChat(saved);
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted)
          setChatHistoryError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setChatHistoryLoading(false);
      });
    return () => controller.abort();
  }, [
    selection.material_id,
    selection.cue.id,
    selection.token.id,
    chatHistoryAttempt,
  ]);
  async function send(event: FormEvent) {
    event.preventDefault();
    if (chatHistoryLoading || chatHistoryError) return;
    const message = question.trim();
    if (!message) return;
    const version = generation.current;
    const payload =
      retry.current?.message === message
        ? retry.current
        : { message, request_id: crypto.randomUUID() };
    retry.current = payload;
    setChatBusy(true);
    setChatError("");
    try {
      const current =
        chat ??
        (await api<Conversation>("/conversations", {
          method: "POST",
          body: JSON.stringify({
            material_id: selection.material_id,
            cue_id: selection.cue.id,
            token_id: selection.token.id,
          }),
        }));
      if (version !== generation.current) return;
      setChat(current);
      await api<ChatAnswer>(`/conversations/${current.id}/messages`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const result = await api<Conversation>(`/conversations/${current.id}`);
      if (version === generation.current) {
        setChat(result);
        setQuestion("");
        retry.current = null;
      }
    } catch (e) {
      if (version === generation.current) setChatError((e as Error).message);
    } finally {
      if (version === generation.current) setChatBusy(false);
    }
  }
  async function correct(event: FormEvent) {
    event.preventDefault();
    setCorrectionError("");
    try {
      await api(
        `/materials/${selection.material_id}/occurrences/${selection.token.id}/lexeme`,
        {
          method: "PUT",
          body: JSON.stringify({
            cue_id: selection.cue.id,
            lemma,
            part_of_speech: pos,
          }),
        },
      );
      onSaved();
    } catch (e) {
      setCorrectionError((e as Error).message);
    }
  }
  return (
    <>
      <FamiliarityEditor token={selection.token} onSaved={onSaved} />
      <div className="contextual-card">
        {cardBusy && <p role="status">Разбираем слово в контексте…</p>}
        {cardError && (
          <div role="alert">
            <p className="warning">{cardError}</p>
            <button
              className="secondary"
              onClick={() => setAttempt(attempt + 1)}
            >
              Повторить разбор
            </button>
          </div>
        )}
        {card && (
          <>
            <h3>{card.card.translation}</h3>
            <p>{card.card.explanation}</p>
            <p className="expression">{card.card.expression}</p>
            <p className="example">Учебный пример: {card.card.example}</p>
            {aiProvenance(card)}
            <button
              className="secondary regenerate-card"
              disabled={cardBusy}
              onClick={() => {
                setCard(null);
                setCardError("");
                setAttempt(attempt + 1);
                setForceRefreshCount(forceRefreshCount + 1);
              }}
            >
              Обновить ответ
            </button>
          </>
        )}
      </div>
      <details>
        <summary>Исправить базовую форму</summary>
        <form onSubmit={(e) => void correct(e)} className="lemma-form">
          <label>
            Базовая форма
            <input
              required
              maxLength={100}
              value={lemma}
              onChange={(e) => setLemma(e.target.value)}
            />
          </label>
          <label>
            Часть речи
            <select value={pos} onChange={(e) => setPos(e.target.value)}>
              {[
                "NOUN",
                "VERB",
                "ADJ",
                "ADV",
                "PROPN",
                "AUX",
                "PRON",
                "ADP",
                "DET",
                "CCONJ",
                "SCONJ",
                "PART",
                "INTJ",
                "NUM",
                "X",
              ].map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          {correctionError && <p className="warning">{correctionError}</p>}
          <button className="secondary">Сохранить форму</button>
        </form>
      </details>
      <button className="discuss" onClick={() => setChatOpen(!chatOpen)}>
        Обсудить с агентом
      </button>
      {chatOpen && (
        <section className="chat">
          <h3>Учебный агент</h3>

          {chat?.messages?.map((turn) => (
            <div key={turn.request_id} className="chat-turn">
              <div className="chat-message user">
                <small>Вы</small>
                <p>{turn.message}</p>
              </div>
              {turn.response && (
                <div className="chat-message assistant">
                  <small>Агент</small>
                  <p>{turn.response.answer}</p>
                  {aiProvenance(turn.response)}
                  {turn.response.educational_example && (
                    <p className="example">
                      Учебный пример: {turn.response.educational_example}
                    </p>
                  )}
                  {turn.response.citations?.map((citation, i) => (
                    <blockquote key={i}>
                      {citation.text}
                      <MaterialCitation citation={citation} currentMaterialId={selection.material_id} onSeek={onSeek} />
                    </blockquote>
                  ))}
                </div>
              )}
            </div>
          ))}
          {chatHistoryLoading && (
            <p role="status">Загружаем предыдущий диалог…</p>
          )}
          {chatHistoryError && (
            <p role="alert" className="warning">
              {chatHistoryError}{" "}
              <button
                type="button"
                className="quiet"
                onClick={() => setChatHistoryAttempt((value) => value + 1)}
              >
                Повторить загрузку диалога
              </button>
            </p>
          )}
          <form onSubmit={(event) => void send(event)}>
            <label>
              Ваш вопрос
              <textarea
                required
                maxLength={4000}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Почему здесь такое значение?"
              />
            </label>
            {chatError && (
              <p role="alert" className="warning">
                {chatError}
              </p>
            )}
            <button
              disabled={
                chatBusy ||
                chatHistoryLoading ||
                !!chatHistoryError ||
                !question.trim()
              }
            >
              {chatBusy
                ? "Агент отвечает…"
                : chatHistoryLoading
                  ? "Загружаем диалог…"
                  : chatError
                    ? "Повторить вопрос"
                    : "Отправить"}
            </button>
          </form>
        </section>
      )}
    </>
  );
}

type DictionaryEntry = Token & { id: string; knowledge: Knowledge };
export function Dictionary() {
  const [tab, setTab] = useState("words");
  const [entries, setEntries] = useState<DictionaryEntry[]>([]);
  const [search, setSearch] = useState("");
  const [score, setScore] = useState("");
  const [ignored, setIgnored] = useState("");
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    const params = new URLSearchParams({ search });
    if (score) params.set("score", score);
    if (ignored) params.set("ignored", ignored);
    api<DictionaryEntry[]>(`/dictionary?${params}`, {
      signal: controller.signal,
    })
      .then((items) => {
        setEntries(items);
        setError("");
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [search, score, ignored, revision]);
  const reload = () => setRevision((value) => value + 1);
  return (
    <section className="dictionary">
      <div className="dictionary-heading">
        <h1>Мои слова</h1>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={reload}
        >
          Обновить словарь
        </button>
      </div>
      <Tabs
        value={tab}
        onChange={setTab}
        label="Личный словарь"
        items={[
          { id: "words", label: "Слова" },
          { id: "expressions", label: "Выражения" },
        ]}
      />
      {tab === "expressions" ? (
        <ExpressionDictionary refreshKey={revision} />
      ) : (
        <>
          <div className="dictionary-filters">
            <label>
              Поиск
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Базовая форма слова"
              />
            </label>
            <label>
              Знакомость
              <select value={score} onChange={(e) => setScore(e.target.value)}>
                <option value="">Все оценки</option>
                <option value="unassessed">Пока не оценено</option>
                {scores.map((s, i) => (
                  <option key={i} value={i}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Исключённые
              <select
                value={ignored}
                onChange={(e) => setIgnored(e.target.value)}
              >
                <option value="">Все слова</option>
                <option value="false">Учитываемые</option>
                <option value="true">Исключённые</option>
              </select>
            </label>
          </div>
          {error && (
            <p role="alert" className="warning">
              {error}
              <button type="button" className="quiet" onClick={reload}>
                Повторить
              </button>
            </p>
          )}
          {busy && <p role="status">Обновляем словарь…</p>}
          {!busy && !entries.length && (
            <p>
              Пока нет подходящих слов. Откройте материал и оцените слово в
              субтитрах.
            </p>
          )}
          <div className="dictionary-grid">
            {entries.map((entry) => (
              <article
                className="dictionary-entry"
                key={entry.lexeme_id ?? entry.id}
              >
                <div className="dictionary-word">
                  <h2>{entry.lemma}</h2>
                  <small>{entry.part_of_speech}</small>
                </div>
                <FamiliarityEditor
                  token={{ ...entry, lexeme_id: entry.lexeme_id ?? entry.id }}
                  onSaved={() => setRevision((value) => value + 1)}
                />
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
