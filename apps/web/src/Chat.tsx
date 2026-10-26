import { learnerStorageKey } from "./session";
import { Fragment, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, time } from "./api";
import type { MaterialSummary } from "./api";
import { FamiliarityEditor, knowledgeLabel } from "./Study";
import { streamReply } from "./chatApi";
import type { Conversation, MessageToken, Reply, Turn } from "./chatApi";
import {
  ExpressionInspector,
  ExpressionToolbar,
  useExpressions,
} from "./Expressions";
import type { ExpressionsView } from "./Expressions";

type InitialContext = {
  material_id: string;
  cue_id?: string;
  token_id?: string;
  title?: string;
};
type PickedWord = { token: MessageToken; text: string; message_id: string };
type Pending = {
  conversation_id: string;
  request_id: string;
  message: string;
  text: string;
};
const topicMessage = "Предложи 2–3 темы для беседы.";

function ReplyWords({
  text,
  tokens,
  selected,
  highlighted,
  onSelect,
}: {
  text: string;
  tokens: MessageToken[];
  selected?: string;
  highlighted?: Set<string>;
  onSelect(token: MessageToken): void;
}) {
  const characters = Array.from(text);
  let end = 0;
  return (
    <>
      {tokens.map((token) => {
        const before = characters.slice(end, token.start_offset).join("");
        end = token.end_offset;
        return (
          <Fragment key={token.id}>
            {before}
            <button
              type="button"
              className={`chat-word${selected === token.id ? " selected" : ""}${highlighted?.has(token.id) ? " expression-highlight" : ""}`}
              title={`${token.surface} · ${knowledgeLabel(token.knowledge)}`}
              onClick={() => onSelect(token)}
            >
              {token.surface}
            </button>
          </Fragment>
        );
      })}
      {characters.slice(end).join("")}
    </>
  );
}

function WordPanel({
  conversationId,
  picked,
  expressions,
  onClose,
  onSaved,
}: {
  conversationId: string;
  picked: PickedWord;
  expressions: ExpressionsView;
  onClose(): void;
  onSaved(): void;
}) {
  const [card, setCard] = useState<{
    card: {
      translation: string;
      explanation: string;
      expression: string;
      example: string;
    };
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [force, setForce] = useState(false);
  const [lemma, setLemma] = useState(picked.token.lemma);
  const [pos, setPos] = useState(picked.token.part_of_speech);
  const [correctionError, setCorrectionError] = useState("");
  async function correct(event: FormEvent) {
    event.preventDefault();
    setCorrectionError("");
    try {
      await api(
        `/conversations/${conversationId}/messages/${picked.message_id}/words/${picked.token.id}/lexeme`,
        { method: "PUT", body: JSON.stringify({ lemma, part_of_speech: pos }) },
      );
      onSaved();
      setAttempt((n) => n + 1);
    } catch (e) {
      setCorrectionError((e as Error).message);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setCard(null);
    setError("");
    api<typeof card>("/cards", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        source: "message",
        conversation_id: conversationId,
        message_id: picked.message_id,
        token_id: picked.token.id,
        force_refresh: force,
      }),
    })
      .then((value) => {
        if (!controller.signal.aborted) setCard(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [conversationId, picked.message_id, picked.token.id, attempt, force]);
  return (
    <aside className="chat-word-panel" aria-label="Слово из сообщения">
      <div className="panel-title">
        <span className="eyebrow">СЛОВО ИЗ БЕСЕДЫ</span>
        <button
          className="quiet"
          onClick={onClose}
          aria-label="Закрыть панель слова"
        >
          ×
        </button>
      </div>
      <ExpressionInspector view={expressions} />
      <details
        className="word-details"
        open={expressions.primary ? undefined : true}
      >
        <summary>Отдельное слово: {picked.token.surface}</summary>
        <h2>{picked.token.surface}</h2>
        <p className="lemma">
          {picked.token.lemma} <small>{picked.token.part_of_speech}</small>
        </p>
        <blockquote>{picked.text}</blockquote>
        <FamiliarityEditor token={picked.token} onSaved={onSaved} />
        {busy && <p role="status">Разбираем слово в контексте…</p>}
        {error && (
          <div role="alert">
            <p className="warning">{error}</p>
            <button
              className="secondary"
              onClick={() => setAttempt((n) => n + 1)}
            >
              Повторить разбор
            </button>
          </div>
        )}
        {card && (
          <div className="contextual-card">
            <h3>{card.card.translation}</h3>
            <p>{card.card.explanation}</p>
            <p className="expression">{card.card.expression}</p>
            <p className="example">{card.card.example}</p>
            <button
              className="quiet"
              disabled={busy}
              onClick={() => {
                setForce(true);
                setAttempt((n) => n + 1);
              }}
            >
              Обновить ответ
            </button>
          </div>
        )}
        <details>
          <summary>Исправить базовую форму</summary>
          <form
            className="lemma-form"
            onSubmit={(event) => void correct(event)}
          >
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
                ].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            {correctionError && (
              <p className="warning" role="alert">
                {correctionError}
              </p>
            )}
            <button className="secondary">Сохранить форму</button>
          </form>
        </details>
      </details>
    </aside>
  );
}

export function Chat({
  materials,
  initialContext,
  onContextConsumed,
}: {
  materials: MaterialSummary[];
  initialContext: InitialContext | null;
  onContextConsumed(): void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Pending | null>(null);
  const [picked, setPicked] = useState<PickedWord | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const selectedRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const alive = useRef(true);
  const loadingVersion = useRef(0);
  const wordVersion = useRef(0);
  const run = useRef<{
    id: string;
    request_id: string;
    controller: AbortController;
  } | null>(null);
  const retry = useRef<{
    id: string;
    request_id: string;
    message: string;
    topics: boolean;
  } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const initial = useRef(initialContext);
  const draftKey = selectedId ?? "new";
  const draft = drafts[draftKey] ?? "";
  const expressionMessage =
    conversation?.messages.find(
      (turn) => turn.request_id === picked?.message_id,
    ) ??
    conversation?.messages
      .filter((turn) => turn.response && turn.status === "complete")
      .at(-1);
  const expressions = useExpressions(
    selectedId && expressionMessage?.response
      ? {
          conversation_id: selectedId,
          message_id: expressionMessage.request_id,
        }
      : null,
  );
  useEffect(() => {
    if (picked && expressionMessage?.request_id === picked.message_id)
      expressions.focus(picked.token.id);
  }, [picked?.token.id, expressionMessage?.request_id]);

  function select(id: string | null) {
    ++loadingVersion.current;
    ++wordVersion.current;
    selectedRef.current = id;
    setSelectedId(id);
    setConversation(null);
    setPicked(null);
    setError("");
    setDeleteConfirm(false);
    setName("");
    if (id) localStorage.setItem(learnerStorageKey("last-chat"), id);
    else localStorage.removeItem(learnerStorageKey("last-chat"));
  }
  async function list() {
    const items = await api<Conversation[]>("/conversations");
    if (alive.current) setConversations(items);
    return items;
  }
  async function load(id: string) {
    const version = ++loadingVersion.current;
    const value = await api<Conversation>(`/conversations/${id}`);
    if (
      alive.current &&
      selectedRef.current === id &&
      version === loadingVersion.current
    ) {
      setConversation(value);
      setName(value.title);
      setPicked((previous) => {
        if (!previous) return null;
        const token = value.messages
          .find((turn) => turn.request_id === previous.message_id)
          ?.tokens?.find((token) => token.id === previous.token.id);
        return token ? { ...previous, token } : null;
      });
    }
  }
  useEffect(() => {
    alive.current = true;
    async function start() {
      try {
        const context = initial.current;
        if (context) {
          const created = await api<Conversation>("/conversations", {
            method: "POST",
            body: JSON.stringify({
              material_id: context.material_id,
              cue_id: context.cue_id,
              token_id: context.token_id,
              title: context.title?.slice(0, 120) || "Разговор о материале",
            }),
          });
          if (!alive.current) return;
          select(created.id);
          onContextConsumed();
        }
        const items = await list();
        if (!alive.current) return;
        if (!selectedRef.current) {
          const saved = localStorage.getItem(learnerStorageKey("last-chat"));
          select(
            items.find((item) => item.id === saved)?.id ?? items[0]?.id ?? null,
          );
        }
      } catch (e) {
        if (alive.current) setError((e as Error).message);
      } finally {
        if (alive.current) setLoading(false);
      }
    }
    void start();
    return () => {
      alive.current = false;
      run.current?.controller.abort();
    };
  }, []);
  useEffect(() => {
    if (!selectedId) return;
    void load(selectedId).catch((e) => {
      if (alive.current && selectedRef.current === selectedId)
        setError((e as Error).message);
    });
  }, [selectedId]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [conversation?.messages.length, pending?.text]);

  async function create() {
    if (busyRef.current) return;
    setError("");
    try {
      const value = await api<Conversation>("/conversations", {
        method: "POST",
        body: "{}",
      });
      select(value.id);
      await list();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function send(message: string, requestId?: string, topics = false) {
    message = message.trim();
    if (!message || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    let id = selectedRef.current;
    try {
      if (!id) {
        const created = await api<Conversation>("/conversations", {
          method: "POST",
          body: JSON.stringify({
            title: topics ? "Новый разговор" : message.slice(0, 80),
          }),
        });
        if (!alive.current) return;
        id = created.id;
        select(id);
        await list();
      }
      const previous = retry.current;
      const request_id =
        requestId ??
        (previous?.id === id &&
        previous.message === message &&
        previous.topics === topics
          ? previous.request_id
          : crypto.randomUUID());
      retry.current = { id, request_id, message, topics };
      run.current = { id, request_id, controller };
      setPending({ conversation_id: id, request_id, message, text: "" });
      if (topics) {
        await api<Reply>(`/conversations/${id}/topics`, {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({ request_id }),
        });
      } else {
        await streamReply(
          id,
          { message, request_id },
          (event) => {
            if (alive.current && event.type === "text")
              setPending((previous) =>
                previous ? { ...previous, text: event.text } : null,
              );
          },
          controller.signal,
        );
      }
      if (alive.current) {
        if (!topics)
          setDrafts((value) => {
            const next = { ...value };
            if ((value[draftKey] ?? "").trim() === message) next[draftKey] = "";
            if (id && (value[id] ?? "").trim() === message) next[id] = "";
            return next;
          });
        retry.current = null;
      }
    } catch (e) {
      if (alive.current)
        setError(
          controller.signal.aborted
            ? "Генерация остановлена. Можно повторить сообщение."
            : (e as Error).message,
        );
    } finally {
      run.current = null;
      busyRef.current = false;
      if (alive.current) {
        if (id) await load(id).catch((e) => setError((e as Error).message));
        await list().catch((e) => setError((e as Error).message));
        setBusy(false);
        setPending(null);
      }
    }
  }
  async function stop() {
    const current = run.current;
    if (!current) return;
    try {
      await api(
        `/conversations/${current.id}/messages/${current.request_id}/cancel`,
        { method: "POST", body: "{}" },
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      current.controller.abort();
    }
  }
  async function saveName(event: FormEvent) {
    event.preventDefault();
    if (!selectedId || !name.trim()) return;
    try {
      await api(`/conversations/${selectedId}`, {
        method: "PATCH",
        body: JSON.stringify({ title: name.trim() }),
      });
      await list();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function remove() {
    if (!selectedId) return;
    try {
      await api(`/conversations/${selectedId}`, { method: "DELETE" });
      select(null);
      await list();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function attach(materialId: string, enabled: boolean) {
    if (!conversation) return;
    const ids = enabled
      ? [...conversation.material_ids, materialId]
      : conversation.material_ids.filter((id) => id !== materialId);
    try {
      await api(`/conversations/${conversation.id}`, {
        method: "PATCH",
        body: JSON.stringify({ material_ids: ids }),
      });
      await load(conversation.id);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function pick(token: MessageToken, turn: Turn) {
    if (!selectedId || !turn.response) return;
    const version = ++wordVersion.current;
    try {
      const selected = await api<MessageToken>(
        `/conversations/${selectedId}/messages/${turn.request_id}/words/${token.id}/selection`,
        { method: "POST", body: "{}" },
      );
      if (version === wordVersion.current)
        setPicked({
          token: { ...token, ...selected },
          message_id: turn.request_id,
          text: turn.response.answer,
        });
    } catch (e) {
      if (version === wordVersion.current) setError((e as Error).message);
    }
  }
  const turns = [...(conversation?.messages ?? [])];
  if (pending && !turns.some((turn) => turn.request_id === pending.request_id))
    turns.push({
      request_id: pending.request_id,
      message: pending.message,
      response: null,
      status: "pending",
    });
  return (
    <section className="practice-page">
      <h1>Практика</h1>
      <div
        className={`practice-layout${picked || expressions.primary || expressions.busy ? " has-word" : ""}`}
      >
        <aside className="conversation-list" aria-label="Сохранённые разговоры">
          <button
            className="new-conversation"
            disabled={busy}
            onClick={() => void create()}
          >
            ＋ Новый разговор
          </button>
          {loading && <p role="status">Загружаем разговоры…</p>}
          {conversations.map((item) => (
            <button
              key={item.id}
              disabled={busy}
              className={`conversation-item${selectedId === item.id ? " active" : ""}`}
              onClick={() => select(item.id)}
            >
              {item.title}
            </button>
          ))}
        </aside>
        <div className="conversation-main">
          {selectedId && (
            <div className="conversation-tools">
              <form onSubmit={(event) => void saveName(event)}>
                <label className="sr-only" htmlFor="conversation-name">
                  Название разговора
                </label>
                <input
                  id="conversation-name"
                  value={name}
                  maxLength={120}
                  disabled={busy}
                  onChange={(e) => setName(e.target.value)}
                />
                <button
                  className="quiet"
                  disabled={busy || !name.trim()}
                  aria-label="Сохранить название"
                >
                  ✓
                </button>
              </form>
              <button
                className="quiet"
                disabled={busy}
                onClick={() => setDeleteConfirm(true)}
              >
                Удалить
              </button>
            </div>
          )}
          {deleteConfirm && (
            <div className="alert">
              <p>Удалить эту беседу? Оценки слов сохранятся.</p>
              <button className="danger" onClick={() => void remove()}>
                Удалить беседу
              </button>
              <button className="quiet" onClick={() => setDeleteConfirm(false)}>
                Отмена
              </button>
            </div>
          )}
          {!turns.length && (
            <div className="conversation-empty">
              <h2>О чём поговорим?</h2>
            </div>
          )}
          <div className="conversation-turns" aria-label="Переписка">
            {turns.map((turn) => (
              <article className="practice-turn" key={turn.request_id}>
                <div className="chat-message user">
                  <small>Вы</small>
                  <p>{turn.message}</p>
                </div>
                {turn.response ? (
                  <div className="chat-message assistant">
                    <small>Учебный агент</small>
                    {turn.response.correction && (
                      <p className="brief-correction">
                        {turn.response.correction}
                      </p>
                    )}
                    <p>
                      <ReplyWords
                        text={turn.response.answer}
                        tokens={turn.tokens ?? []}
                        selected={picked?.token.id}
                        highlighted={
                          expressionMessage?.request_id === turn.request_id
                            ? expressions.highlighted
                            : undefined
                        }
                        onSelect={(token) => {
                          if (
                            expressionMessage?.request_id === turn.request_id &&
                            expressions.intercept(token.id)
                          )
                            return;
                          void pick(token, turn);
                        }}
                      />
                    </p>
                    {expressionMessage?.request_id === turn.request_id && (
                      <ExpressionToolbar
                        view={expressions}
                        order={(turn.tokens ?? []).map((token) => token.id)}
                      />
                    )}
                    {turn.response.educational_example && (
                      <p className="example">
                        {turn.response.educational_example}
                      </p>
                    )}
                    {turn.response.suggested_topics?.map((topic) => (
                      <button
                        className="topic-choice"
                        key={topic}
                        disabled={busy}
                        onClick={() => void send(topic)}
                      >
                        {topic} ↗
                      </button>
                    ))}
                    {turn.response.citations?.map((citation, index) => (
                      <blockquote key={index}>
                        {citation.text}
                        <a
                          href={citation.youtube_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {citation.title} · {time(citation.start_ms)} ↗
                        </a>
                      </blockquote>
                    ))}
                    {turn.response.model && (
                      <details className="ai-provenance">
                        <summary>Об ответе</summary>
                        <p>
                          {turn.response.model} ·{" "}
                          {turn.response.reasoning_effort ?? "по умолчанию"}
                        </p>
                      </details>
                    )}
                  </div>
                ) : pending?.request_id === turn.request_id ? (
                  <div className="chat-message assistant">
                    <small>Учебный агент</small>
                    <p role="status">{pending.text || "Печатает…"}</p>
                  </div>
                ) : (
                  <div className="unfinished-turn">
                    <p>
                      {turn.status === "cancelled"
                        ? "Генерация остановлена."
                        : "Ответ не завершён."}
                    </p>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        void send(
                          turn.message,
                          turn.request_id,
                          turn.message === topicMessage,
                        )
                      }
                    >
                      Повторить сообщение
                    </button>
                  </div>
                )}
              </article>
            ))}
            <div ref={bottom} />
          </div>
          {error && (
            <div className="warning" role="alert">
              {error}
            </div>
          )}
          <form
            className="conversation-composer"
            onSubmit={(event) => {
              event.preventDefault();
              void send(draft);
            }}
          >
            <label className="sr-only" htmlFor="chat-message">
              Ваше сообщение
            </label>
            <textarea
              id="chat-message"
              value={draft}
              maxLength={4000}
              disabled={busy}
              placeholder="What would you like to talk about?"
              onChange={(e) =>
                setDrafts((value) => ({ ...value, [draftKey]: e.target.value }))
              }
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <div className="actions">
              <button type="submit" disabled={busy || !draft.trim()}>
                Отправить ↗
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void send(topicMessage, undefined, true)}
              >
                Предложить тему
              </button>
              {busy && (
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void stop()}
                >
                  Остановить
                </button>
              )}
            </div>
          </form>
          {conversation && materials.length > 0 && (
            <details className="conversation-materials">
              <summary>
                Материалы беседы ({conversation.material_ids.length})
              </summary>
              <p className="help">
                Недавние учебные материалы учитываются автоматически. Здесь
                можно добавить другие.
              </p>
              {materials.map((material) => (
                <label key={material.id}>
                  <input
                    type="checkbox"
                    disabled={busy}
                    checked={conversation.material_ids.includes(material.id)}
                    onChange={(e) => void attach(material.id, e.target.checked)}
                  />
                  {material.title}
                </label>
              ))}
            </details>
          )}
        </div>
        {picked && selectedId && (
          <WordPanel
            key={picked.token.id}
            conversationId={selectedId}
            picked={picked}
            expressions={expressions}
            onClose={() => {
              ++wordVersion.current;
              setPicked(null);
              expressions.clearFocus();
            }}
            onSaved={() =>
              void load(selectedId).catch((e) => setError((e as Error).message))
            }
          />
        )}
        {!picked && (expressions.primary || expressions.busy) && (
          <aside
            className="chat-word-panel"
            aria-label="Выражение из сообщения"
          >
            <button className="quiet" onClick={expressions.clearFocus}>
              Закрыть выражение
            </button>
            <ExpressionInspector view={expressions} />
          </aside>
        )}
      </div>
    </section>
  );
}
