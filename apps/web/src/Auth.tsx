import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { setSession, stopSession } from "./session";
import type { Account, Session } from "./session";

function sessionChanged() {
  stopSession();
  try {
    const channel = new BroadcastChannel("language-tutor-session");
    channel.postMessage("changed");
    channel.close();
    localStorage.setItem("language-tutor-session-change", crypto.randomUUID());
  } catch {
    /* Focus validation also catches a changed session. */
  }
  window.location.reload();
}

export function AuthBoundary({
  children,
}: {
  children(user: Account): ReactNode;
}) {
  const [session, updateSession] = useState<Session | null>(null);
  const [error, setError] = useState("");
  const [attempt, retry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const reload = () => {
      stopSession();
      window.location.reload();
    };
    async function load() {
      try {
        const value = await api<Session>("/auth/session", {
          signal: controller.signal,
        });
        if (active) {
          setSession(value);
          updateSession(value);
          setError("");
        }
      } catch (e) {
        if (active && (e as Error).name !== "AbortError")
          setError((e as Error).message);
      }
    }
    void load();
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel("language-tutor-session");
      channel.onmessage = reload;
    } catch {
      /* Storage events remain available. */
    }
    const storage = (event: StorageEvent) => {
      if (event.key === "language-tutor-session-change") reload();
    };
    window.addEventListener("storage", storage);
    window.addEventListener("session-expired", reload);
    return () => {
      active = false;
      controller.abort();
      channel?.close();
      window.removeEventListener("storage", storage);
      window.removeEventListener("session-expired", reload);
    };
  }, [attempt]);
  useEffect(() => {
    if (!session?.user) return;
    let active = true;
    const validate = async () => {
      try {
        const latest = await api<Session>("/auth/session");
        if (active && latest.session_key !== session.session_key) {
          stopSession();
          window.location.reload();
        }
      } catch {
        /* Existing API error states handle network loss. */
      }
    };
    const visible = () => {
      if (document.visibilityState === "visible") void validate();
    };
    const restored = (event: PageTransitionEvent) => {
      if (event.persisted) {
        stopSession();
        window.location.reload();
      }
    };
    window.addEventListener("pageshow", restored);
    window.addEventListener("focus", visible);
    document.addEventListener("visibilitychange", visible);
    const timer = window.setInterval(() => void validate(), 30_000);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("pageshow", restored);
      window.removeEventListener("focus", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [session]);
  if (session?.user) return children(session.user);
  if (!session)
    return (
      <div className="auth-loading" role="status">
        {error ? (
          <>
            <p role="alert">{error}</p>
            <button onClick={() => retry(attempt + 1)}>Повторить</button>
          </>
        ) : (
          "Открываем Language Tutor…"
        )}
      </div>
    );
  return (
    <SignIn first={!!session.first_account} legacy={!!session.legacy_profile} />
  );
}

function SignIn({ first, legacy }: { first: boolean; legacy: boolean }) {
  const [register, setRegister] = useState(first);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [adopt, setAdopt] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (register && password !== confirmation) {
      setError("Пароли не совпадают.");
      return;
    }
    setBusy(true);
    try {
      await api(register ? "/auth/register" : "/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username,
          password,
          ...(register ? { adopt_existing: legacy && adopt } : {}),
        }),
      });
      sessionChanged();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  return (
    <div className="auth-page">
      <section className="auth-story" aria-label="Language Tutor">
        <a href="/" className="auth-brand">
          <span className="brand-mark">lt</span>
          <span>
            Language <strong>Tutor</strong>
          </span>
        </a>
      </section>
      <section className="auth-panel">
        <div className="auth-card">
          <h2>{register ? "Создать аккаунт" : "Вход"}</h2>
          <div className="auth-tabs" aria-label="Вход или регистрация">
            <button
              type="button"
              aria-pressed={!register}
              disabled={busy}
              onClick={() => {
                setRegister(false);
                setError("");
                setPassword("");
                setConfirmation("");
              }}
            >
              Вход
            </button>
            <button
              type="button"
              aria-pressed={register}
              disabled={busy}
              onClick={() => {
                setRegister(true);
                setError("");
                setPassword("");
                setConfirmation("");
              }}
            >
              Регистрация
            </button>
          </div>
          <form onSubmit={(event) => void submit(event)}>
            <label htmlFor="auth-username">Имя пользователя</label>
            <input
              id="auth-username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              minLength={register ? 3 : 1}
              maxLength={32}
              pattern={register ? "[A-Za-z0-9_.\\-]{3,32}" : undefined}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              aria-describedby={register ? "username-help" : undefined}
              disabled={busy}
            />
            {register && (
              <small id="username-help">
                3–32 символа: латинские буквы, цифры, точка, дефис или _.
              </small>
            )}
            <label htmlFor="auth-password">Пароль</label>
            <input
              id="auth-password"
              type="password"
              autoComplete={register ? "new-password" : "current-password"}
              required
              minLength={register ? 12 : 1}
              maxLength={128}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={busy}
              aria-describedby={register ? "password-help" : undefined}
            />
            {register && (
              <>
                <small id="password-help">
                  От 12 символов. Можно использовать длинную фразу.
                </small>
                <label htmlFor="auth-confirm">Повторите пароль</label>
                <input
                  id="auth-confirm"
                  type="password"
                  autoComplete="new-password"
                  required
                  maxLength={128}
                  value={confirmation}
                  onChange={(e) => setConfirmation(e.target.value)}
                  disabled={busy}
                />
              </>
            )}
            {register && legacy && (
              <label className="auth-adopt">
                <input
                  type="checkbox"
                  required
                  checked={adopt}
                  onChange={(e) => setAdopt(e.target.checked)}
                  disabled={busy}
                />
                <span>
                  Это моя установка. Перенести прежние учебные данные и
                  подключение AI в мою учётную запись. Исходная копия
                  сохранится.
                </span>
              </label>
            )}
            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}
            <button className="auth-submit" disabled={busy}>
              {busy
                ? "Подождите…"
                : register
                  ? "Создать учётную запись"
                  : "Войти"}
              <span aria-hidden="true">→</span>
            </button>
          </form>
        </div>
      </section>
    </div>
  );
}

export function AccountMenu({ user }: { user: Account }) {
  const [editing, setEditing] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (editing) dialog.current?.showModal();
  }, [editing]);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function logout() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api("/auth/logout", { method: "POST", body: "{}" });
      sessionChanged();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  async function change(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    if (next !== repeat) {
      setError("Пароли не совпадают.");
      return;
    }
    setBusy(true);
    try {
      await api("/auth/password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      sessionChanged();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  return (
    <div className="account-menu">
      <details className="account-dropdown">
        <summary className="profile" aria-label={`Аккаунт ${user.username}`}>
          <span className="avatar">{user.username[0].toUpperCase()}</span>
          <span className="account-name">{user.username}</span>
        </summary>
        <div className="account-actions">
          <button
            onClick={() => {
              setEditing(true);
              setError("");
            }}
            disabled={busy}
          >
            Сменить пароль
          </button>
          <button onClick={() => void logout()} disabled={busy}>
            Выйти
          </button>
        </div>
      </details>
      {error && !editing && (
        <p role="alert" className="auth-error">
          {error}
        </p>
      )}
      {editing && (
        <dialog
          ref={dialog}
          className="auth-password-dialog"
          aria-labelledby="password-title"
          onCancel={(event) => {
            if (busy) event.preventDefault();
            else {
              setEditing(false);
              setCurrent("");
              setNext("");
              setRepeat("");
              setError("");
            }
          }}
        >
          <h2 id="password-title">Сменить пароль</h2>
          <p>
            После смены пароля нужно будет войти заново на всех устройствах.
          </p>
          <form onSubmit={(event) => void change(event)}>
            <label>
              Текущий пароль
              <input
                autoFocus
                type="password"
                autoComplete="current-password"
                required
                maxLength={128}
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </label>
            <label>
              Новый пароль
              <input
                type="password"
                autoComplete="new-password"
                required
                minLength={12}
                maxLength={128}
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </label>
            <small>От 12 символов.</small>
            <label>
              Повторите новый пароль
              <input
                type="password"
                autoComplete="new-password"
                required
                maxLength={128}
                value={repeat}
                onChange={(e) => setRepeat(e.target.value)}
              />
            </label>
            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}
            <div className="account-actions">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setCurrent("");
                  setNext("");
                  setRepeat("");
                  setError("");
                }}
              >
                Отмена
              </button>
              <button className="auth-submit" disabled={busy}>
                {busy ? "Сохраняем…" : "Сохранить пароль"}
              </button>
            </div>
          </form>
        </dialog>
      )}
    </div>
  );
}
