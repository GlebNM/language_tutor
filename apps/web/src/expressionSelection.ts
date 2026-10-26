import type { Knowledge } from "./api.ts";

export type ExpressionKind = "phrasal_verb" | "idiom" | "collocation";
export type ExpressionSource = {material_id: string} | {conversation_id: string; message_id: string};
export type ExpressionContext = {
  text: string; cue_id: string; material_id?: string; start_ms?: number; end_ms?: number;
  youtube_video_id?: string; title?: string;
  anchors?: {start_offset: number; end_offset: number}[];
};
export type ExpressionEntry = {
  id: string; canonical: string; kind: ExpressionKind; sense_key: string;
  translation: string; explanation: string; example: string; usage: string;
  saved: boolean; knowledge: Knowledge; occurrence_id?: string; token_ids?: string[];
  contexts?: ExpressionContext[]; material_id?: string; conversation_id?: string; message_id?: string;
  examples?: {id: string; material_id?: string; contexts: ExpressionContext[]}[];
};
export type ExpressionState = {
  available: boolean; occurrences: ExpressionEntry[];
  job: {status: "queued" | "running" | "completed" | "failed"; completed_batches: number;
    total_batches: number; error: string | null} | null;
  counts: {total: number; unassessed: number; learning: number};
};

export function sourceKey(source: ExpressionSource | null): string {
  if (!source) return "";
  return "material_id" in source ? `material:${source.material_id}`
    : `message:${source.conversation_id}:${source.message_id}`;
}

export function phrasalForToken(entries: ExpressionEntry[], tokenId: string | null): ExpressionEntry[] {
  // Familiarity and ignored never suppress the explicit phrasal-verb selection.
  return tokenId ? entries.filter(entry => entry.kind === "phrasal_verb" && entry.token_ids?.includes(tokenId)) : [];
}

export function expressionHighlights(entries: ExpressionEntry[], tokenId: string | null, explicitId: string | null): Set<string> {
  const chosen = explicitId ? entries.filter(entry => entry.occurrence_id === explicitId)
    : phrasalForToken(entries, tokenId);
  return new Set(chosen.flatMap(entry => entry.token_ids ?? []));
}

export function orderedTokens(selected: string[], order: string[]): string[] {
  const included = new Set(selected);
  return order.filter(id => included.has(id));
}
