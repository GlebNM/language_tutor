import { sessionFetch } from "./session.ts";
export type Knowledge = {
  score: number | null;
  ignored: boolean;
  source: string | null;
  provisional: boolean;
  status: string;
};
export type Token = {
  id: string;
  material_id: string;
  cue_id: string;
  lexeme_id: string;
  surface: string;
  start_offset: number;
  end_offset: number;
  lemma: string;
  language: string;
  part_of_speech: string;
  uncertain: boolean;
  vocabulary?: {
    eligible: boolean;
    automatically_eligible: boolean;
    reason: "filler" | "stretched" | "proper_name" | null;
  };
  knowledge?: Knowledge;
};
export type Cue = {
  id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  original_text: string;
  tokens: Token[];
};
export type MaterialSummary = {
  id: string;
  title: string;
  youtube_video_id: string;
  youtube_url: string;
  language: string;
  source: string;
  is_complete: boolean;
  version: number;
  adapter_version: string;
  previous_version_id?: string | null;
};
export type Material = MaterialSummary & { cues: Cue[]; difficulty?: Difficulty };
export type Difficulty = {
  [key: string]: unknown;
};
export type Selection = {
  material_id: string;
  cue: Cue;
  token: Token;
  youtube_url: string;
};
export type Connection = {
  status: string;
  authorization_url?: string;
  message?: string;
  error?: string | null;
  [key: string]: unknown;
};
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await sessionFetch(`/api${path}`, {
      ...options,
      headers: {
        ...(options?.body ? { "Content-Type": "application/json" } : {}),
        ...options?.headers,
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error(
      "Не удалось связаться с сервером. Проверьте подключение и повторите запрос.",
    );
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new Error("Сервер вернул непонятный ответ. Повторите запрос.");
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown }).detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string"
          ? detail.message
        : "Не удалось выполнить запрос. Проверьте данные и повторите.",
    );
  }
  return body as T;
}
export function time(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
