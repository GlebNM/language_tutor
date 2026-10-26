import { AuthBoundary, AccountMenu } from "./Auth";
import type { Account } from "./session";
import { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { FormEvent, ReactNode } from "react";
import { api, time } from "./api";
import type {
  Connection,
  Cue,
  Material,
  MaterialSummary,
  Selection,
  Token,
} from "./api";
import { Player } from "./Player";
import type { PlayerHandle } from "./Player";
import {
  AIModelSettings,
  Dictionary,
  DifficultyPanel,
  LearningPanel,
  VideoVocabulary,
  knowledgeLabel,
} from "./Study";
import { Diagnostics } from "./Diagnostics";
import { LearningSettings, ViewingReview, useViewing } from "./Viewing";
import { Chat } from "./Chat";
import { Captions } from "./Captions";
import { Progress } from "./Progress";
import { Discovery, YouTubeSearchSettings } from "./Discovery";
import { usePlaybackProgress } from "./usePlaybackProgress";
import {
  ExpressionInspector,
  ExpressionList,
  ExpressionToolbar,
  useExpressions,
} from "./Expressions";
import { Icon, Modal, Tabs } from "./UI";
import type { IconName } from "./UI";
import { Transcript } from "./Transcript";
import { readRoute, routeHash } from "./navigation";
import "./style.css";

function CueWords({
  cue,
  selected,
  highlighted,
  onSelect,
}: {
  cue: Cue;
  selected?: string;
  highlighted?: Set<string>;
  onSelect(token: Token): void;
}) {
  const parts: ReactNode[] = [];
  const characters = Array.from(cue.text);
  let end = 0;
  for (const token of cue.tokens) {
    parts.push(characters.slice(end, token.start_offset).join(""));
    parts.push(
      <button
        key={token.id}
        className={`word score-${token.vocabulary?.eligible === false || token.knowledge?.ignored ? "ignored" : (token.knowledge?.score ?? "null")} ${selected === token.id ? "selected" : ""}${highlighted?.has(token.id) ? " expression-highlight" : ""}`}
        onClick={() => onSelect(token)}
        title={`${token.surface} · ${knowledgeLabel(token.knowledge)}`}
      >
        {token.surface}
      </button>,
    );
    end = token.end_offset;
  }
  parts.push(characters.slice(end).join(""));
  return <>{parts}</>;
}
function WordLearning({
  selection,
  phrasal,
  busy,
  onSaved,
  onSeek,
}: {
  selection: Selection;
  phrasal: boolean;
  busy: boolean;
  onSaved(): void;
  onSeek(ms: number): void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (busy)
    return <p role="status">Уточняем выражение перед разбором слова…</p>;
  const content = (
    <LearningPanel selection={selection} onSaved={onSaved} onSeek={onSeek} />
  );
  return phrasal ? (
    <details
      className="word-details"
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>Отдельное слово: {selection.token.surface}</summary>
      {expanded && content}
    </details>
  ) : (
    content
  );
}
function App({ user }: { user: Account }) {
  const [library, setLibrary] = useState<MaterialSummary[]>([]);
  const [material, setMaterial] = useState<Material | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [position, setPosition] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [settings, setSettings] = useState(false);
  const [section, setSection] = useState("materials");
  const [studyTab, setStudyTab] = useState("transcript");
  const [librarySearch, setLibrarySearch] = useState("");
  const [largeVideo, setLargeVideo] = useState(false);
  const [chatContext, setChatContext] = useState<{
    material_id: string;
    cue_id?: string;
    token_id?: string;
    title?: string;
  } | null>(null);
  const [resetConfirm, setResetConfirm] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [authorizationUrl, setAuthorizationUrl] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [proof, setProof] = useState("");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [importMode, setImportMode] = useState<"youtube" | "file">("youtube");
  const [improveAutomatic, setImproveAutomatic] = useState(true);
  const [complete, setComplete] = useState(true);
  const [importing, setImporting] = useState(false);
  const [refreshingMaterial, setRefreshingMaterial] = useState(false);
  const active = useRef(0);
  const selectionRequest = useRef(0);
  const libraryRefreshRequest = useRef(0);
  const materialRefreshRequest = useRef(0);
  const connectionRequest = useRef(0);
  const aiActionPending = useRef(false);
  const player = useRef<PlayerHandle>(null);
  const inspectorClose = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const pageContent = useRef<HTMLDivElement>(null);
  const viewing = useViewing(material, refreshMaterial);
  const playback = usePlaybackProgress(material?.id);
  const expressions = useExpressions(
    material ? { material_id: material.id } : null,
  );
  const routeHandler = useRef(() => {});
  routeHandler.current = () => {
    const route = readRoute(window.location.hash);
    if (route.materialId) void open(route.materialId, false);
    else navigate(route.section, false);
  };
  const expressionOrder = useMemo(
    () =>
      material?.cues.flatMap((cue) => cue.tokens.map((token) => token.id)) ??
      [],
    [material],
  );
  useEffect(() => {
    const label =
      material?.title ??
      (
        {
          materials: "Библиотека",
          discovery: "Поиск видео",
          dictionary: "Словарь",
          chat: "Практика",
          progress: "Прогресс",
          diagnostics: "Проверка словаря",
          settings: "Настройки",
        } as Record<string, string>
      )[section];
    document.title = `${label} · Language Tutor`;
    pageContent.current?.scrollTo(0, 0);
  }, [section, material?.id]);
  useEffect(() => {
    if (selection) inspectorClose.current?.focus({ preventScroll: true });
  }, [selection?.token.id]);
  async function refresh() {
    const request = ++libraryRefreshRequest.current;
    try {
      const result = await api<MaterialSummary[]>("/materials");
      if (request === libraryRefreshRequest.current) setLibrary(result);
    } catch (error) {
      if (request === libraryRefreshRequest.current) throw error;
    }
  }
  useEffect(() => {
    refresh()
      .then(() => routeHandler.current())
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
    const restore = () => routeHandler.current();
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  useEffect(() => {
    if (!settings) return;
    const controller = new AbortController();
    let polling = false;
    const poll = async () => {
      if (polling || aiActionPending.current) return;
      polling = true;
      const request = ++connectionRequest.current;
      try {
        const result = await api<Connection>("/ai/connection", {
          signal: controller.signal,
        });
        if (request === connectionRequest.current && !aiActionPending.current)
          setConnection(result);
      } catch (e) {
        if (
          (e as Error).name !== "AbortError" &&
          request === connectionRequest.current
        )
          setError((e as Error).message);
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      controller.abort();
      ++connectionRequest.current;
      window.clearInterval(timer);
    };
  }, [settings]);
  useEffect(() => {
    const offline = () =>
      setNotice(
        "Нет соединения с интернетом. Загруженные субтитры доступны для чтения.",
      );
    const online = () => setNotice("Соединение восстановлено.");
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, []);
  async function open(id: string, updateRoute = true) {
    if (updateRoute) {
      const hash = routeHash({ section: "materials", materialId: id });
      if (window.location.hash !== hash)
        window.history.pushState(null, "", hash);
    }
    const request = ++active.current;
    ++materialRefreshRequest.current;
    setRefreshingMaterial(false);
    ++selectionRequest.current;
    player.current?.pause();
    setMaterial(null);
    setSelection(null);
    setPosition(0);
    setSettings(false);
    setSection("materials");
    setStudyTab("transcript");
    setError("");
    setLoading(true);
    try {
      const result = await api<Material>(`/materials/${id}`);
      if (request === active.current) setMaterial(result);
    } catch (e) {
      if (request === active.current) setError((e as Error).message);
    } finally {
      if (request === active.current) setLoading(false);
    }
  }
  async function select(cue: Cue, token: Token, fromVideo = false) {
    if (!material) return;
    if (document.activeElement instanceof HTMLElement)
      previousFocus.current = document.activeElement;
    player.current?.pause();
    if (expressions.intercept(token.id)) return;
    expressions.focus(token.id);
    const materialRequest = active.current;
    const request = ++selectionRequest.current;
    setSelection(null);
    setError("");
    try {
      const result = fromVideo
        ? await viewing.select(cue, token)
        : await api<Selection>("/activity/word-selections", {
            method: "POST",
            body: JSON.stringify({
              material_id: material.id,
              cue_id: cue.id,
              token_id: token.id,
              request_id: crypto.randomUUID(),
            }),
          });
      if (
        request === selectionRequest.current &&
        materialRequest === active.current
      )
        setSelection(result);
    } catch (e) {
      if (
        request === selectionRequest.current &&
        materialRequest === active.current
      )
        setError((e as Error).message);
    }
  }
  function closeSelection() {
    const restoreFocus = document.activeElement?.closest(".selection-panel");
    ++selectionRequest.current;
    setSelection(null);
    expressions.clearFocus();
    if (restoreFocus)
      requestAnimationFrame(() => {
        if (previousFocus.current?.isConnected)
          previousFocus.current.focus({ preventScroll: true });
      });
  }
  async function refreshMaterial() {
    if (!material) return;
    const activeRequest = active.current;
    const request = ++materialRefreshRequest.current;
    setRefreshingMaterial(true);
    try {
      const result = await api<Material>(`/materials/${material.id}`);
      if (
        activeRequest !== active.current ||
        request !== materialRefreshRequest.current
      )
        return;
      setMaterial(result);
      setSelection((previous) => {
        if (!previous) return previous;
        for (const cue of result.cues) {
          const token = cue.tokens.find(
            (item) => item.id === previous.token.id,
          );
          if (token)
            return {
              material_id: result.id,
              cue,
              token,
              youtube_url: result.youtube_url,
            };
        }
        return null;
      });
    } catch (error) {
      if (
        activeRequest === active.current &&
        request === materialRefreshRequest.current
      )
        throw error;
    } finally {
      if (request === materialRefreshRequest.current)
        setRefreshingMaterial(false);
    }
  }
  function navigate(next: string, updateRoute = true) {
    if (updateRoute) {
      const hash = routeHash({ section: next });
      if (window.location.hash !== hash)
        window.history.pushState(null, "", hash);
    }
    ++active.current;
    closeSelection();
    setMaterial(null);
    setLoading(false);
    ++materialRefreshRequest.current;
    setRefreshingMaterial(false);
    setSettings(next === "settings");
    setSection(next);
    setError("");
  }
  async function resetData() {
    setAiBusy(true);
    setError("");
    try {
      await api("/data/reset", {
        method: "POST",
        body: JSON.stringify({ confirmation: "Удалить мои данные" }),
      });
      playback.reset();
      navigate("materials");
      setResetConfirm(false);
      setConnection(null);
      setAuthorizationUrl("");
      setProof("");
      await refresh();
      setNotice("Учебные данные удалены. Подписка отключена.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAiBusy(false);
    }
  }
  async function demo() {
    try {
      const result = await api<Material>("/materials/demo", {
        method: "POST",
        body: "{}",
      });
      await refresh();
      await open(result.id);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function beginImport(replace = false) {
    setReplacing(replace);
    setShowImport(true);
    setUrl(replace && material ? material.youtube_url : "");
    setTitle(replace && material ? material.title : "");
    setFile(null);
    setImportMode(replace ? "file" : "youtube");
    setImproveAutomatic(true);
    setComplete(true);
    setError("");
  }
  async function importTrack(event: FormEvent) {
    event.preventDefault();
    if (importMode === "youtube") {
      setImporting(true);
      setError("");
      try {
        const result = await api<Material>("/materials/youtube", {
          method: "POST",
          body: JSON.stringify({
            youtube_url: url,
            title,
            improve_automatic: improveAutomatic,
          }),
        });
        await refresh();
        setShowImport(false);
        await open(result.id);
        setNotice("Субтитры YouTube сохранены. Можно начинать просмотр.");
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setImporting(false);
      }
      return;
    }
    if (!file) {
      setError("Выберите файл SRT или VTT.");
      return;
    }
    if (file.size > 2_000_000) {
      setError("Файл должен быть не больше 2 МБ.");
      return;
    }
    const format = file.name.toLowerCase().endsWith(".vtt")
      ? "vtt"
      : file.name.toLowerCase().endsWith(".srt")
        ? "srt"
        : null;
    if (!format) {
      setError("Выберите файл с расширением .srt или .vtt.");
      return;
    }
    setImporting(true);
    setError("");
    try {
      const bytes = await file.arrayBuffer();
      const content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      const result = await api<Material>(
        replacing && material
          ? `/materials/${material.id}/track`
          : "/materials",
        {
          method: replacing ? "PUT" : "POST",
          body: JSON.stringify({
            youtube_url: url,
            title,
            format,
            content,
            is_complete: complete,
          }),
        },
      );
      await refresh();
      setShowImport(false);
      await open(result.id);
      setNotice(
        replacing
          ? "Дорожка сохранена. Предыдущая версия доступна в библиотеке."
          : "Материал сохранён в библиотеке.",
      );
    } catch (e) {
      setError(
        e instanceof TypeError
          ? "Не удалось прочитать UTF-8 или связаться с сервером. Проверьте файл и подключение."
          : (e as Error).message,
      );
    } finally {
      setImporting(false);
    }
  }
  async function aiAction(action: "start" | "cancel" | "disconnect" | "proof") {
    aiActionPending.current = true;
    ++connectionRequest.current;
    setAiBusy(true);
    setError("");
    setProof("");
    try {
      if (action === "proof") {
        const result = await api<{
          card: { translation: string; explanation: string; example: string };
          model?: string;
          reasoning_effort?: string | null;
        }>("/ai/proof", { method: "POST" });
        setProof(
          [
            "Подписка работает.",
            ...(result.model
              ? [
                  `Модель: ${result.model} · reasoning: ${Object.prototype.hasOwnProperty.call(result, "reasoning_effort") ? (result.reasoning_effort ?? "по умолчанию") : "не записан"}`,
                ]
              : []),
            result.card.translation,
            result.card.explanation,
            result.card.example,
          ].join("\n\n"),
        );
      } else {
        const path =
          action === "disconnect"
            ? "/ai/connection"
            : `/ai/connection/${action}`;
        const result = await api<Connection>(path, {
          method: action === "disconnect" ? "DELETE" : "POST",
        });
        ++connectionRequest.current;
        setConnection(result);
        setAuthorizationUrl(result.authorization_url ?? "");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      aiActionPending.current = false;
      setAiBusy(false);
    }
  }
  const currentCues =
    material?.cues.filter(
      (cue) => cue.start_ms <= position && cue.end_ms > position,
    ) ?? [];
  const source = (value: string) =>
    ({
      demo: "Подготовленный пример",
      srt: "Импорт SRT",
      vtt: "Импорт VTT",
      "youtube-authored": "Авторская дорожка",
      "youtube-automatic": "Автоматическая дорожка",
      "youtube-improved": "Автосубтитры · обработаны AI",
    })[value] ?? value;
  const status = connection
    ? ({
        disconnected: "Не подключена",
        connecting: "Ожидаем вход",
        connected: "Подключена",
        reauth_required: "Требуется повторный вход",
        limit_reached: "Лимит подписки",
        authorizing: "Ожидаем вход",
      }[connection.status] ?? connection.status)
    : "Загрузка…";
  return (
    <div className="app">
      <aside className="sidebar">
        <a
          href="#materials"
          className="brand"
          aria-label="Language Tutor — библиотека"
          onClick={(event) => {
            event.preventDefault();
            navigate("materials");
          }}
        >
          <span className="brand-mark">
            <Icon name="play" size={19} />
          </span>
          <span>Language Tutor</span>
        </a>
        <nav aria-label="Основные разделы">
          {(
            [
              ["materials", "library", "Библиотека"],
              ["discovery", "search", "Найти видео"],
              ["dictionary", "words", "Словарь"],
              ["chat", "chat", "Практика"],
              ["progress", "chart", "Прогресс"],
              ["diagnostics", "assessment", "Проверка словаря"],
            ] as Array<[string, IconName, string]>
          ).map(([id, icon, label]) => (
            <button
              key={id}
              className={`nav${section === id ? " active" : ""}`}
              aria-label={label}
              title={label}
              aria-current={section === id ? "page" : undefined}
              onClick={() => navigate(id)}
            >
              <Icon name={icon} />
              <span>{label}</span>
              {id === "materials" && (
                <span className="count">{library.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button
            className={`nav${settings ? " active" : ""}`}
            aria-label="Настройки"
            title="Настройки"
            aria-current={settings ? "page" : undefined}
            onClick={() => navigate("settings")}
          >
            <Icon name="settings" />
            <span>Настройки</span>
          </button>
          <AccountMenu user={user} />
        </div>
      </aside>
      <main
        className={
          material ? "watch-page" : section === "chat" ? "chat-page" : ""
        }
      >
        <header className="app-header">
          {material ? (
            <button
              className="quiet back-link"
              onClick={() => navigate("materials")}
            >
              <Icon name="back" size={16} />
              Библиотека
            </button>
          ) : (
            <span className="header-location">
              {
                (
                  {
                    materials: "Библиотека",
                    discovery: "Поиск видео",
                    dictionary: "Словарь",
                    chat: "Практика",
                    progress: "Прогресс",
                    diagnostics: "Проверка словаря",
                    settings: "Настройки",
                  } as Record<string, string>
                )[section]
              }
            </span>
          )}
          <span className="language-pair">
            English <span>→</span> Русский
          </span>
        </header>
        <div ref={pageContent} className="page-content">
          {error && (
            <div className="alert error" role="alert">
              {error}
              <button
                className="quiet"
                onClick={() => {
                  setError("");
                  const retry = material ? refreshMaterial() : refresh();
                  void retry.catch((e) => setError((e as Error).message));
                }}
              >
                Обновить данные
              </button>
            </div>
          )}
          {notice && (
            <div className="alert" role="status">
              {notice}
              <button
                className="quiet"
                onClick={() => setNotice("")}
                aria-label="Закрыть сообщение"
              >
                ×
              </button>
            </div>
          )}
          {settings ? (
            <section className="settings">
              <h1>Настройки</h1>
              <LearningSettings />
              <AIModelSettings />
              <YouTubeSearchSettings />
              <div className="settings-card">
                <h2>Подписка ChatGPT</h2>
                <p role="status">{status}</p>
                {connection?.error && (
                  <p>
                    {(
                      {
                        callback_port_in_use:
                          "Порт подключения занят. Освободите порт или измените настройку callback.",
                        oauth_denied: "Вы отменили подключение подписки.",
                        plan_permission_missing:
                          "Не разрешено использование подписки для AI. Отключите подключение и повторите вход с разрешением.",
                        oauth_exchange_failed:
                          "Не удалось завершить подключение. Попробуйте ещё раз.",
                        refresh_failed: "Подключите подписку заново.",
                        subscription_limit:
                          "Достигнут лимит подписки. Повторите позже.",
                        authorization_expired:
                          "Разрешение истекло. Подключите подписку заново.",
                      } as Record<string, string>
                    )[connection.error] ??
                      "Не удалось подключить подписку. Попробуйте ещё раз."}
                  </p>
                )}
                <div className="actions">
                  {connection?.status !== "connected" &&
                    connection?.status !== "connecting" && (
                      <button
                        disabled={aiBusy}
                        onClick={() => void aiAction("start")}
                      >
                        Подключить ChatGPT
                      </button>
                    )}
                  {connection?.status === "connecting" && (
                    <button
                      className="secondary"
                      disabled={aiBusy}
                      onClick={() => void aiAction("cancel")}
                    >
                      Отменить вход
                    </button>
                  )}
                  {connection && connection.status !== "disconnected" && (
                    <button
                      className="quiet"
                      disabled={aiBusy}
                      onClick={() => void aiAction("disconnect")}
                    >
                      Отключить
                    </button>
                  )}
                </div>
                {authorizationUrl && connection?.status === "connecting" && (
                  <a
                    className="auth-link"
                    href={authorizationUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Открыть страницу входа ↗
                  </a>
                )}
                <details>
                  <summary>Помощь с подключением</summary>
                  <p>
                    Запустите Compose с compose.oauth.yaml. Порт
                    AI_CALLBACK_PORT (по умолчанию 1456) должен быть свободен.
                  </p>
                </details>
                {connection?.status === "connected" && (
                  <button
                    className="secondary"
                    disabled={aiBusy}
                    onClick={() => void aiAction("proof")}
                  >
                    Проверить подключение
                  </button>
                )}
                {proof && <p className="proof">{proof}</p>}
              </div>
              <div className="settings-card danger-zone">
                <h2>Удалить мои данные</h2>
                <p>
                  Удаляются материалы, словарь, ручные оценки, диагностика,
                  диалоги, кэш подбора видео, сохранённый ключ YouTube и
                  подключение подписки. Это действие нельзя отменить.
                </p>
                {resetConfirm ? (
                  <>
                    <p>
                      Подтвердите полное удаление учебных данных этой локальной
                      установки.
                    </p>
                    <div className="actions">
                      <button
                        className="danger"
                        disabled={aiBusy}
                        onClick={() => void resetData()}
                      >
                        Удалить все данные
                      </button>
                      <button
                        className="secondary"
                        disabled={aiBusy}
                        onClick={() => setResetConfirm(false)}
                      >
                        Отмена
                      </button>
                    </div>
                  </>
                ) : (
                  <button
                    className="danger"
                    onClick={() => setResetConfirm(true)}
                  >
                    Удалить мои данные…
                  </button>
                )}
              </div>
            </section>
          ) : section === "discovery" ? (
            <Discovery
              onOpen={async (id) => {
                await refresh();
                await open(id);
              }}
            />
          ) : section === "progress" ? (
            <Progress
              beforeLoad={playback.flush}
              onMaterial={(id) => void open(id)}
            />
          ) : section === "chat" ? (
            <Chat
              materials={library}
              initialContext={chatContext}
              onContextConsumed={() => setChatContext(null)}
            />
          ) : section === "dictionary" ? (
            <Dictionary />
          ) : section === "diagnostics" ? (
            <Diagnostics
              onApplied={() =>
                void refresh().catch((e) => setError((e as Error).message))
              }
            />
          ) : loading ? (
            <div className="empty" role="status">
              Загружаем материалы…
            </div>
          ) : material ? (
            <div
              className={`watch-workspace${largeVideo ? " large-video" : ""}`}
            >
              <div className="view-heading">
                <h1 title={material.title}>{material.title}</h1>
                <div className="actions">
                  {viewing.enabled && (
                    <button
                      className="secondary"
                      onClick={() => {
                        player.current?.pause();
                        void viewing.finish();
                      }}
                    >
                      Завершить просмотр
                    </button>
                  )}
                  <button
                    className="quiet icon-button"
                    title={largeVideo ? "Компактное видео" : "Увеличить видео"}
                    aria-label={
                      largeVideo ? "Компактное видео" : "Увеличить видео"
                    }
                    aria-pressed={largeVideo}
                    onClick={() => setLargeVideo((value) => !value)}
                  >
                    <Icon name="expand" />
                  </button>
                  <details className="material-menu">
                    <summary aria-label="Действия с видео">
                      <Icon name="more" />
                    </summary>
                    <div className="menu-popover">
                      <button
                        className="quiet"
                        onClick={() => {
                          setChatContext({
                            material_id: material.id,
                            title: material.title,
                          });
                          navigate("chat");
                        }}
                      >
                        Обсудить в практике
                      </button>
                      <button
                        className="quiet"
                        disabled={refreshingMaterial}
                        onClick={() =>
                          void refreshMaterial().catch((e) =>
                            setError(e.message),
                          )
                        }
                      >
                        Обновить данные
                      </button>
                      <button
                        className="quiet"
                        onClick={() => beginImport(true)}
                      >
                        Заменить субтитры
                      </button>
                    </div>
                  </details>
                </div>
              </div>
              {viewing.error && !viewing.open && (
                <p className="warning" role="alert">
                  {viewing.error}
                </p>
              )}
              {viewing.notice && (
                <p className="alert" role="status">
                  {viewing.notice}
                </p>
              )}
              <ViewingReview viewing={viewing} />
              <div
                className={`watch-grid${selection || expressions.primary || expressions.busy ? " has-selection" : ""}`}
              >
                <section className="watch" aria-label="Видео">
                  <Player
                    key={material.id}
                    ref={player}
                    videoId={material.youtube_video_id}
                    cues={material.cues}
                    onTime={setPosition}
                    onPlayback={(sample) => {
                      viewing.sample(sample);
                      playback.sample(sample);
                    }}
                    onEnded={() => void viewing.finish()}
                  />
                  {playback.error && (
                    <p className="warning" role="alert">
                      Время просмотра не сохранено.{" "}
                      <button
                        className="quiet"
                        onClick={() => void playback.flush().catch(() => {})}
                      >
                        Повторить
                      </button>
                    </p>
                  )}
                  <div className="live-caption" aria-live="off">
                    <div className="caption-time">{time(position)}</div>
                    <div className="live-text">
                      {currentCues.length ? (
                        currentCues.map((cue) => (
                          <p key={cue.id}>
                            <CueWords
                              cue={cue}
                              selected={selection?.token.id}
                              highlighted={expressions.highlighted}
                              onSelect={(token) =>
                                void select(cue, token, true)
                              }
                            />
                          </p>
                        ))
                      ) : (
                        <p className="muted">
                          {position
                            ? "…"
                            : "Субтитры появятся при воспроизведении"}
                        </p>
                      )}
                    </div>
                  </div>
                  <DifficultyPanel value={material.difficulty} />
                  <details className="video-details">
                    <summary>О видео и субтитрах</summary>
                    <p>
                      {source(material.source)} · версия {material.version}
                      {!material.is_complete && " · неполная дорожка"}
                    </p>
                    <Captions
                      key={material.id}
                      material={material}
                      onLibraryChanged={() =>
                        void refresh().catch((e) => setError(e.message))
                      }
                      onOpen={(id) => {
                        void refresh().catch((e) => setError(e.message));
                        void open(id);
                      }}
                    />
                    {material.source === "demo" && (
                      <p className="attribution">
                        Elephants Dream © 2006 Blender Foundation / Netherlands
                        Media Art Institute ·{" "}
                        <a
                          href="https://orange.blender.org/blog/creative-commons-license-2/"
                          target="_blank"
                          rel="noreferrer"
                        >
                          CC BY 2.5
                        </a>
                        . Субтитры:{" "}
                        <a
                          href="https://commons.wikimedia.org/w/index.php?title=TimedText:Elephants_Dream.ogv.en.srt&oldid=1204466026"
                          target="_blank"
                          rel="noreferrer"
                        >
                          Wikimedia Commons contributors
                        </a>
                        , CC BY-SA 4.0; исправлен разделитель реплики 28.
                      </p>
                    )}
                  </details>
                </section>
                <section
                  className="study-pane"
                  aria-label="Работа с материалом"
                >
                  <Tabs
                    value={studyTab}
                    onChange={(value) => {
                      closeSelection();
                      setStudyTab(value);
                    }}
                    label="Материал"
                    items={[
                      { id: "transcript", label: "Субтитры" },
                      { id: "words", label: "Слова" },
                      { id: "expressions", label: "Выражения" },
                    ]}
                  />
                  <div
                    className="study-content"
                    role="tabpanel"
                    aria-label={
                      studyTab === "transcript"
                        ? "Субтитры"
                        : studyTab === "words"
                          ? "Слова"
                          : "Выражения"
                    }
                  >
                    {studyTab === "transcript" ? (
                      <>
                        <ExpressionToolbar
                          view={expressions}
                          order={expressionOrder}
                          onBegin={closeSelection}
                        />
                        <Transcript
                          cues={material.cues}
                          activeId={currentCues[0]?.id}
                          onSeek={(ms) => {
                            player.current?.pause();
                            player.current?.seek(ms);
                          }}
                          renderWords={(cue) => (
                            <CueWords
                              cue={cue}
                              selected={selection?.token.id}
                              highlighted={expressions.highlighted}
                              onSelect={(token) => void select(cue, token)}
                            />
                          )}
                        />
                      </>
                    ) : studyTab === "words" ? (
                      <VideoVocabulary
                        key={material.id}
                        cues={material.cues}
                        isComplete={material.is_complete}
                        onSaved={() =>
                          void refreshMaterial().catch((e) =>
                            setError(e.message),
                          )
                        }
                        onOccurrence={(cue, token) => {
                          player.current?.pause();
                          player.current?.seek(cue.start_ms);
                          void select(cue, token);
                        }}
                      />
                    ) : (
                      <ExpressionList
                        view={expressions}
                        onOpen={(entry) => {
                          const cue = material.cues.find((c) =>
                            c.tokens.some((t) => t.id === entry.token_ids?.[0]),
                          );
                          const token = cue?.tokens.find(
                            (t) => t.id === entry.token_ids?.[0],
                          );
                          if (cue && token) {
                            player.current?.pause();
                            player.current?.seek(cue.start_ms);
                            void select(cue, token);
                            expressions.choose(entry);
                          }
                        }}
                      />
                    )}
                  </div>
                </section>
                {(selection || expressions.primary || expressions.busy) && (
                  <aside
                    className="selection-panel"
                    aria-label="Разбор слова"
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        event.stopPropagation();
                        closeSelection();
                      }
                    }}
                  >
                    <div className="inspector-heading">
                      <button className="quiet" onClick={closeSelection}>
                        <Icon name="back" size={16} />К материалу
                      </button>
                      <button
                        ref={inspectorClose}
                        className="quiet icon-button"
                        onClick={closeSelection}
                        aria-label="Закрыть выбор"
                      >
                        <Icon name="close" size={16} />
                      </button>
                    </div>
                    <div className="inspector-content">
                      <ExpressionInspector
                        view={expressions}
                        onSeek={(ms) => {
                          player.current?.pause();
                          player.current?.seek(ms);
                        }}
                      />
                      {selection && (
                        <>
                          <h2 className="selected-word">
                            {selection.token.surface}
                          </h2>
                          <p className="lemma">
                            {selection.token.lemma}{" "}
                            <span>{selection.token.part_of_speech}</span>
                          </p>
                          {selection.token.uncertain && (
                            <p className="warning">
                              Не удалось точно определить форму слова.
                            </p>
                          )}
                          <blockquote>{selection.cue.text}</blockquote>
                          <button
                            className="timestamp"
                            onClick={() => {
                              player.current?.pause();
                              player.current?.seek(selection.cue.start_ms);
                            }}
                          >
                            {time(selection.cue.start_ms)}
                          </button>
                          <WordLearning
                            key={`${selection.material_id}:${selection.cue.id}:${selection.token.id}`}
                            selection={selection}
                            phrasal={!!expressions.primary}
                            busy={expressions.busy}
                            onSaved={() =>
                              void refreshMaterial().catch((e) =>
                                setError(e.message),
                              )
                            }
                            onSeek={(ms) => {
                              player.current?.pause();
                              player.current?.seek(ms);
                            }}
                          />
                        </>
                      )}
                    </div>
                    <button
                      className="continue"
                      onClick={() => {
                        closeSelection();
                        player.current?.play();
                      }}
                    >
                      <Icon name="play" size={16} />
                      Продолжить
                    </button>
                  </aside>
                )}
              </div>
            </div>
          ) : (
            <section className="library">
              <div className="library-heading">
                <h1>
                  Библиотека{" "}
                  <span className="heading-count">{library.length}</span>
                </h1>
                <button onClick={() => beginImport()}>
                  <Icon name="plus" size={17} />
                  Добавить видео
                </button>
              </div>
              {library.length > 0 && (
                <div className="library-toolbar">
                  <label className="search-field">
                    <Icon name="search" size={17} />
                    <input
                      aria-label="Поиск в библиотеке"
                      value={librarySearch}
                      onChange={(event) => setLibrarySearch(event.target.value)}
                      placeholder="Найти видео в библиотеке"
                    />
                  </label>
                </div>
              )}
              {library.length ? (
                <div className="material-grid">
                  {library
                    .filter((item) =>
                      item.title
                        .toLowerCase()
                        .includes(librarySearch.toLowerCase()),
                    )
                    .map((item) => (
                      <button
                        className="material-card"
                        key={item.id}
                        onClick={() => void open(item.id)}
                      >
                        <div className="cover">
                          <img
                            alt=""
                            loading="lazy"
                            src={`https://i.ytimg.com/vi/${item.youtube_video_id}/hqdefault.jpg`}
                          />
                          <span className="play-disc">
                            <Icon name="play" size={22} />
                          </span>
                        </div>
                        <div className="card-body">
                          <h2>{item.title}</h2>
                          <p>
                            {source(item.source)}
                            {!item.is_complete && " · фрагмент"}
                          </p>
                        </div>
                      </button>
                    ))}
                </div>
              ) : (
                <div className="empty">
                  <h2>Начните с интересного видео</h2>
                  <p>
                    Вставьте ссылку YouTube — субтитры загрузятся автоматически.
                  </p>
                  <button onClick={() => beginImport()}>Добавить видео</button>
                  <button className="secondary" onClick={() => void demo()}>
                    Открыть подготовленный пример
                  </button>
                </div>
              )}
              {library.length > 0 &&
                !library.some((item) =>
                  item.title
                    .toLowerCase()
                    .includes(librarySearch.toLowerCase()),
                ) && (
                  <div className="empty">
                    Ничего не найдено{" "}
                    <button
                      className="quiet"
                      onClick={() => setLibrarySearch("")}
                    >
                      Сбросить поиск
                    </button>
                  </div>
                )}
            </section>
          )}
        </div>
      </main>
      {showImport && (
        <Modal
          labelledBy="import-title"
          onClose={() => setShowImport(false)}
          busy={importing}
        >
          <div className="panel-title">
            <span />
            <button
              className="quiet close"
              disabled={importing}
              onClick={() => setShowImport(false)}
              aria-label="Закрыть импорт"
            >
              ×
            </button>
          </div>
          <h2 id="import-title">
            {replacing ? "Заменить субтитры" : "Добавить видео"}
          </h2>

          {!replacing && (
            <div
              className="actions"
              role="group"
              aria-label="Способ добавления"
            >
              <button
                type="button"
                className={importMode === "youtube" ? "" : "secondary"}
                disabled={importing}
                onClick={() => setImportMode("youtube")}
              >
                По ссылке YouTube
              </button>
              <button
                type="button"
                className={importMode === "file" ? "" : "secondary"}
                disabled={importing}
                onClick={() => setImportMode("file")}
              >
                Из файла SRT/VTT
              </button>
            </div>
          )}
          <form onSubmit={(event) => void importTrack(event)}>
            <label>
              Ссылка YouTube
              <input
                required
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://www.youtube.com/watch?v=…"
                readOnly={replacing}
              />
            </label>
            <label>
              Название (необязательно)
              <input
                maxLength={200}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder={
                  importMode === "youtube"
                    ? "Загрузится с YouTube"
                    : "Как назвать материал?"
                }
              />
            </label>
            {importMode === "file" ? (
              <>
                <label className="file-field">
                  Файл SRT / VTT
                  <input
                    required
                    type="file"
                    accept=".srt,.vtt"
                    onChange={(event) =>
                      setFile(event.target.files?.[0] ?? null)
                    }
                  />
                  <small>
                    UTF-8 · до 2 МБ · 10 000 реплик · 200 000 символов
                  </small>
                </label>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={complete}
                    onChange={(event) => setComplete(event.target.checked)}
                  />
                  Дорожка содержит все реплики видео
                </label>
                <p className="help">
                  Снимите отметку для фрагмента. Приложение не может проверить
                  полноту автоматически.
                </p>
              </>
            ) : (
              <>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={improveAutomatic}
                    onChange={(event) =>
                      setImproveAutomatic(event.target.checked)
                    }
                  />
                  Автоматически улучшать автосубтитры с AI
                </label>
                <p className="help">
                  Нужна подписка ChatGPT. Исходные субтитры сохранятся.
                </p>
              </>
            )}
            {error && (
              <p className="warning" role="alert">
                {error}
              </p>
            )}
            <button className="submit" disabled={importing}>
              {importing
                ? "Обрабатываем субтитры…"
                : replacing
                  ? "Сохранить новую версию"
                  : "Добавить в библиотеку"}
            </button>
          </form>
        </Modal>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <AuthBoundary>{(user) => <App key={user.id} user={user} />}</AuthBoundary>,
);
