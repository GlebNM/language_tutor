export type Account = { id: string; username: string };
export type Session = {
  user: Account | null;
  session_key: string | null;
  first_account?: boolean;
  legacy_profile?: boolean;
};
let sessionKey: string | null = null;
let accountId = "anonymous";
let blocked = false;

export function setSession(session: Session) {
  sessionKey = session.session_key;
  accountId = session.user?.id ?? "anonymous";
  blocked = false;
}
export function learnerStorageKey(key: string): string {
  return `language-tutor.${accountId}.${key}`;
}
export function stopSession() { blocked = true; }
export async function sessionFetch(path: string, options?: RequestInit): Promise<Response> {
  const isAuth = path.startsWith("/api/auth/");
  if (blocked && !isAuth) throw new Error("Сессия изменилась. Войдите заново.");
  const key = sessionKey;
  const headers = new Headers(options?.headers);
  if (key) headers.set("X-Session-Key", key);
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  if (response.status === 401 && !isAuth && typeof window !== "undefined") {
    blocked = true;
    window.dispatchEvent(new Event("session-expired"));
  }
  return response;
}
