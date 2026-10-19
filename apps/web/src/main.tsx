import { useEffect, useRef, useState } from "react";
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
import { Dictionary, DifficultyPanel, LearningPanel, knowledgeLabel } from "./Study";
import { Diagnostics } from "./Diagnostics";
import "./style.css";

function CueWords({
  cue,
  selected,
  onSelect,
}: {
  cue: Cue;
  selected?: string;
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
        className={`word score-${token.knowledge?.ignored ? "ignored" : token.knowledge?.score ?? "null"} ${selected === token.id ? "selected" : ""}`}
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
function App() {
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
  const [resetConfirm, setResetConfirm] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [authorizationUrl, setAuthorizationUrl] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [proof, setProof] = useState("");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [complete, setComplete] = useState(true);
  const [importing, setImporting] = useState(false);
  const active = useRef(0);
  const selectionRequest = useRef(0);
  const player = useRef<PlayerHandle>(null);
  const refresh = () => api<MaterialSummary[]>("/materials").then(setLibrary);
  useEffect(() => {
    refresh()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    if (!settings) return;
    const controller = new AbortController();
    const poll = () =>
      api<Connection>("/ai/connection", { signal: controller.signal })
        .then(setConnection)
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        });
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      controller.abort();
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
  async function open(id: string) {
    const request = ++active.current;
    ++selectionRequest.current;
    player.current?.pause();
    setMaterial(null);
    setSelection(null);
    setPosition(0);
    setSettings(false);
    setSection("materials");
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
  async function select(cue: Cue, token: Token) {
    if (!material) return;
    player.current?.pause();
    const materialRequest = active.current;
    const request = ++selectionRequest.current;
    setSelection(null);
    setError("");
    try {
      const result = await api<Selection>(
        `/materials/${material.id}/selection?cue_id=${cue.id}&token_id=${token.id}`,
      );
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
    ++selectionRequest.current;
    setSelection(null);
  }
  async function refreshMaterial() {
    if (!material) return;
    const request = active.current;
    const result = await api<Material>(`/materials/${material.id}`);
    if (request !== active.current) return;
    setMaterial(result);
    setSelection(previous => {
      if(!previous) return previous;
      for(const cue of result.cues) {
        const token = cue.tokens.find(item => item.id === previous.token.id);
        if(token) return {material_id:result.id,cue,token,youtube_url:result.youtube_url};
      }
      return null;
    });
  }
  function navigate(next: string) {
    ++active.current; closeSelection(); setMaterial(null); setLoading(false);
    setSettings(next === "settings"); setSection(next); setError("");
  }
  async function resetData() {
    setAiBusy(true); setError("");
    try {
      await api("/data/reset", {method:"POST", body:JSON.stringify({confirmation:"Удалить мои данные"})});
      navigate("materials"); setResetConfirm(false); setConnection(null); setAuthorizationUrl(""); setProof(""); await refresh();
      setNotice("Учебные данные удалены. Подписка отключена.");
    } catch (e) {setError((e as Error).message);}
    finally {setAiBusy(false);}
  }
  async function demo() {
    try {const result = await api<Material>("/materials/demo", {method:"POST",body:"{}"}); await refresh(); await open(result.id);}
    catch(e) {setError((e as Error).message);}
  }
  function beginImport(replace = false) {
    setReplacing(replace);
    setShowImport(true);
    setUrl(replace && material ? material.youtube_url : "");
    setTitle(replace && material ? material.title : "");
    setFile(null);
    setComplete(true);
    setError("");
  }
  async function importTrack(event: FormEvent) {
    event.preventDefault();
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
    setAiBusy(true);
    setError("");
    setProof("");
    try {
      if (action === "proof") {
        const result = await api<{
          card: { translation: string; explanation: string; example: string };
        }>("/ai/proof", { method: "POST" });
        setProof(
          [
            "Подписка работает.",
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
        setConnection(result);
        setAuthorizationUrl(result.authorization_url ?? "");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
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
          href="#"
          className="brand"
          onClick={(event) => {
            event.preventDefault();
            ++active.current;
            closeSelection();
            navigate("materials");
          }}
        >
          <span className="brand-mark">lt</span>
          <span>
            Language
            <br />
            <strong>Tutor</strong>
          </span>
        </a>
        <p className="eyebrow">ВАШ АНГЛИЙСКИЙ</p>
        <nav>
          <button
            className={section === "materials" ? "nav active" : "nav"}
            onClick={() => {
              navigate("materials");
            }}
          >
            ▤ <span>Материалы</span>
            <span className="count">{library.length}</span>
          </button>
          <button className={section === "dictionary" ? "nav active" : "nav"} onClick={() => navigate("dictionary")}>Aa <span>Мои слова</span></button>
          <button className={section === "diagnostics" ? "nav active" : "nav"} onClick={() => navigate("diagnostics")}>✦ <span>Диагностика</span></button>
          <button
            className={settings ? "nav active" : "nav"}
            onClick={() => {
              navigate("settings");
            }}
          >
            ⚙ <span>Настройки AI</span>
          </button>
        </nav>
        <div className="profile">
          <span className="avatar">Я</span>
          <div>
            <strong>Мой профиль</strong>
            <small>Английский · объяснения на русском</small>
          </div>
          <span className="online-dot" title="Локальный профиль" />
        </div>
        <p className="local-note">
          Данные сохраняются
          <br />
          на вашем компьютере.
        </p>
      </aside>
      <main>
        <header>
          <span>
            {settings
              ? "Настройки"
              : material
                ? "Материалы / Просмотр"
                : section === "dictionary" ? "Мои слова" : section === "diagnostics" ? "Диагностика" : "Моя библиотека"}
          </span>
          <span className="local-pill">
            <i /> Локальное приложение
          </span>
        </header>
        {error && (
          <div className="alert error" role="alert">
            {error}
            <button
              className="quiet"
              onClick={() => {
                setError("");
                void refresh().catch((e) => setError(e.message));
              }}
            >
              Повторить
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
            <p className="eyebrow">ЛИЧНАЯ ПОДПИСКА</p>
            <h1>Настройки AI</h1>
            <p className="lede">
              Подключите ChatGPT для учебных объяснений. Материалы
              доступны сразу.
            </p>
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
                <button
                  disabled={aiBusy}
                  onClick={() => void aiAction("start")}
                >
                  Continue with ChatGPT
                </button>
                <button
                  className="secondary"
                  disabled={aiBusy}
                  onClick={() => void aiAction("cancel")}
                >
                  Отменить вход
                </button>
                <button
                  className="quiet"
                  disabled={aiBusy}
                  onClick={() => void aiAction("disconnect")}
                >
                  Отключить
                </button>
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
              <p className="help">
                Для OAuth запустите Compose с файлом compose.oauth.yaml. Порт
                AI_CALLBACK_PORT (по умолчанию 1456) должен быть свободен.
              </p>
              <button
                className="secondary"
                disabled={aiBusy}
                onClick={() => void aiAction("proof")}
              >
                Проверить подключение
              </button>
              {proof && <p className="proof">{proof}</p>}
              <h3>Адрес backend для расширения</h3>
              <code>{window.location.origin}</code>
            </div>
            <div className="settings-card danger-zone"><h2>Удалить мои данные</h2><p>Удаляются материалы, словарь, ручные оценки, диагностика, диалоги и подключение подписки. Это действие нельзя отменить.</p>
              {resetConfirm ? <><p>Подтвердите полное удаление учебных данных этой локальной установки.</p><div className="actions"><button className="danger" disabled={aiBusy} onClick={() => void resetData()}>Удалить все данные</button><button className="secondary" disabled={aiBusy} onClick={() => setResetConfirm(false)}>Сохранить данные</button></div></> : <button className="danger" onClick={() => setResetConfirm(true)}>Удалить мои данные…</button>}
            </div>
          </section>
        ) : section === "dictionary" ? <Dictionary /> : section === "diagnostics" ? <Diagnostics onApplied={() => void refresh()} /> : loading ? (
          <div className="empty" role="status">
            Загружаем материалы…
          </div>
        ) : material ? (
          <>
            <div className="view-heading">
              <div>
                <p className="eyebrow">ПРОСМОТР · EN → RU</p>
                <h1>{material.title}</h1>
                <p className="subtitle">
                  {source(material.source)} · версия {material.version}
                  {!material.is_complete &&
                    " · Неполная дорожка — сложность всего видео недоступна"}
                </p>
              </div>
              <div className="actions"><button className="secondary" onClick={() => void refreshMaterial().catch(e => setError(e.message))}>Обновить данные</button><button className="secondary" onClick={() => beginImport(true)}>Заменить дорожку</button></div>
            </div>
            <div className="watch-grid">
              <section className="watch">
                <DifficultyPanel value={material.difficulty} />
                <Player
                  key={material.id}
                  ref={player}
                  videoId={material.youtube_video_id}
                  onTime={setPosition}
                />
                <div className="live-caption" aria-live="off">
                  <span className="eyebrow">СУБТИТРЫ · {time(position)}</span>
                  <div className="live-text">
                    {currentCues.length ? (
                      currentCues.map((cue) => (
                        <p key={cue.id}>
                          <CueWords
                            cue={cue}
                            selected={selection?.token.id}
                            onSelect={(token) => void select(cue, token)}
                          />
                        </p>
                      ))
                    ) : (
                      <p className="muted">Здесь появится текущая реплика.</p>
                    )}
                  </div>
                  <p className="help">
                    Нажмите на слово, чтобы остановить видео и сохранить
                    контекст.
                  </p>
                </div>
                {material.source === "demo" && (
                  <p className="help">
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
                <div className="transcript-heading">
                  <h2>Текст материала</h2>
                  <span>{material.cues.length} реплик</span>
                </div>
                <div className="transcript">
                  {material.cues.map((cue) => (
                    <div
                      className={`cue ${currentCues.some((current) => current.id === cue.id) ? "current" : ""}`}
                      key={cue.id}
                    >
                      <button
                        className="timestamp"
                        onClick={() => player.current?.seek(cue.start_ms)}
                        aria-label={`Перейти к ${time(cue.start_ms)}`}
                      >
                        {time(cue.start_ms)}
                      </button>
                      <p>
                        <CueWords
                          cue={cue}
                          selected={selection?.token.id}
                          onSelect={(token) => void select(cue, token)}
                        />
                      </p>
                    </div>
                  ))}
                </div>
              </section>
              <aside className="selection-panel">
                {selection ? (
                  <>
                    <div className="panel-title">
                      <span className="eyebrow">ВЫБРАННОЕ СЛОВО</span>
                      <button
                        className="quiet close"
                        onClick={closeSelection}
                        aria-label="Закрыть выбор"
                      >
                        ×
                      </button>
                    </div>
                    <h2 className="selected-word">{selection.token.surface}</h2>
                    <p className="lemma">
                      {selection.token.lemma}{" "}
                      <span>{selection.token.part_of_speech}</span>
                    </p>
                    {selection.token.uncertain && (
                      <p className="warning">
                        Разбор неуверенный: использована форма из субтитров.
                      </p>
                    )}
                    <blockquote>{selection.cue.text}</blockquote>
                    <button
                      className="timestamp"
                      onClick={() =>
                        player.current?.seek(selection.cue.start_ms)
                      }
                    >
                      ↗ {time(selection.cue.start_ms)}
                    </button>
                    <LearningPanel key={selection.token.id} selection={selection} onSaved={() => void refreshMaterial().catch(e => setError(e.message))} onSeek={milliseconds => player.current?.seek(milliseconds)} />
                    <div className="selection-note">
                      <strong>Контекст закреплён</strong>
                      <p>
                        Можно вернуться к этому времени или продолжить просмотр,
                        когда будете готовы.
                      </p>
                    </div>
                    <button
                      className="continue"
                      onClick={() => {
                        closeSelection();
                        player.current?.play();
                      }}
                    >
                      Продолжить просмотр ▷
                    </button>
                  </>
                ) : (
                  <div className="select-empty">
                    <span className="select-icon">Aa</span>
                    <h2>Разберём слово?</h2>
                    <p>
                      Нажмите на слово в субтитрах. Видео остановится, а
                      выбранная реплика останется перед вами.
                    </p>
                    <span className="mini-label">
                      Вы решаете, когда продолжить
                    </span>
                  </div>
                )}
              </aside>
            </div>
          </>
        ) : (
          <section className="library">
            <div className="library-heading">
              <div>
                <p className="eyebrow">УЧИТЕСЬ НА ТОМ, ЧТО ИНТЕРЕСНО</p>
                <h1>
                  Ваши материалы<span className="accent">.</span>
                </h1>
                <p className="lede">
                  Смотрите видео. Замечайте слова. Понимайте больше.
                </p>
              </div>
              <button onClick={() => beginImport()}>
                ＋ Добавить материал
              </button>
            </div>
            <div className="section-label">
              <h2>Продолжить изучение</h2>
              <span>{library.length} материалов</span>
            </div>
            {library.length ? (
              <div className="material-grid">
                {library.map((item) => (
                  <button
                    className="material-card"
                    key={item.id}
                    onClick={() => void open(item.id)}
                  >
                    <div className="cover">
                      <img
                        alt=""
                        src={`https://i.ytimg.com/vi/${item.youtube_video_id}/hqdefault.jpg`}
                      />
                      <span className="play-disc">▷</span>
                      <span className="cover-label">ENGLISH</span>
                    </div>
                    <div className="card-body">
                      <p className="eyebrow">
                        {source(item.source)} · V{item.version}
                      </p>
                      <h2>{item.title}</h2>
                      <p>
                        {item.is_complete
                          ? "Дорожка отмечена как полная"
                          : "Неполная дорожка"}
                      </p>
                      <span className="card-link">
                        Открыть материал <span>↗</span>
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className="empty">
                <h2>Начните с интересного видео</h2>
                <p>Добавьте ссылку YouTube и файл SRT/VTT.</p>
                <button onClick={() => beginImport()}>Добавить материал</button>
                <button className="secondary" onClick={() => void demo()}>Открыть подготовленный пример</button>
              </div>
            )}
            <div className="library-tip">
              <span>✦</span>
              <div>
                <strong>В своём темпе</strong>
                <p>
                  Клик по слову ставит видео на паузу. Продолжайте, когда будете
                  готовы.
                </p>
              </div>
            </div>
          </section>
        )}
        <footer>
          Language Tutor <span>Один профиль · ваши материалы · ваш темп</span>
        </footer>
      </main>
      {showImport && (
        <div className="modal-backdrop">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="import-title"
          >
            <div className="panel-title">
              <p className="eyebrow">БИБЛИОТЕКА</p>
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
              {replacing ? "Заменить дорожку" : "Добавить материал"}
            </h2>
            <p>Ссылка YouTube и английские субтитры для этого видео.</p>
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
                Название
                <input
                  maxLength={200}
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="Как назвать материал?"
                />
              </label>
              <label className="file-field">
                Файл SRT / VTT
                <input
                  required
                  type="file"
                  accept=".srt,.vtt"
                  onChange={(event) => setFile(event.target.files?.[0] ?? null)}
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
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
