import { sessionFetch } from "./session.ts";
import type { Token } from "./api";

export type MessageToken = Omit<Token, "material_id" | "cue_id"> & {
  conversation_id: string;
  message_id: string;
};
export type Reply = {
  answer: string;
  correction?: string | null;
  educational_example?: string | null;
  suggested_topics?: string[];
  citations?: Array<{text: string; title: string; start_ms: number; youtube_url: string}>;
  model?: string;
  reasoning_effort?: string | null;
};
export type Turn = {
  request_id: string;
  message: string;
  response: Reply | null;
  status: "pending" | "complete" | "failed" | "cancelled";
  tokens?: MessageToken[];
};
export type Conversation = {
  id: string;
  title: string;
  material_ids: string[];
  messages: Turn[];
};
export type ChatEvent =
  | {type: "text" | "status"; text: string}
  | {type: "complete"; response: Reply}
  | {type: "cancelled"};

export async function streamReply(
  conversationId: string,
  incoming: {message: string; request_id: string},
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await sessionFetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages/stream`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(incoming), signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error("Не удалось связаться с сервером. Повторите запрос.");
  }
  if (!response.ok) {
    const result = await response.json().catch(() => null);
    throw new Error(typeof result?.detail === "string" ? result.detail : "Не удалось получить ответ.");
  }
  if (!response.body) throw new Error("Сервер не открыл поток ответа. Повторите запрос.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  function receive(block: string) {
    const data = block.split(/\r?\n/).filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).replace(/^ /, "")).join("\n");
    if (!data) return;
    let event;
    try { event = JSON.parse(data); }
    catch { throw new Error("Сервер вернул непонятный поток ответа. Повторите запрос."); }
    if (event.type === "error") throw new Error(typeof event.detail === "string" ? event.detail : "Ошибка AI. Повторите запрос.");
    if (event.type === "complete" && typeof event.response?.answer === "string") finished = true;
    else if (event.type === "cancelled") finished = true;
    else if (!["text", "status"].includes(event.type) || typeof event.text !== "string") {
      throw new Error("Сервер вернул непонятный поток ответа. Повторите запрос.");
    }
    onEvent(event);
  }
  try {
    while (true) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value, {stream: !chunk.done});
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) receive(block);
      if (buffer.length > 1_000_000) throw new Error("Ответ слишком большой. Повторите запрос.");
      if (chunk.done) break;
    }
    if (buffer.trim()) receive(buffer);
    if (!finished) throw new Error("Ответ прерван. Повторите запрос.");
  } finally {
    await reader.cancel().catch(() => {});
  }
}
