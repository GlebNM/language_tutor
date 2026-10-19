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
};
export type Material = MaterialSummary & { cues: Cue[] };
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
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      ...(options?.body ? { "Content-Type": "application/json" } : {}),
      ...options?.headers,
    },
  });
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
        : "Не удалось выполнить запрос. Проверьте данные и повторите.",
    );
  }
  return body as T;
}
export function time(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
