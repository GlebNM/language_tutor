import { time } from "./api";
import type { Citation } from "./api";
import { routeHash } from "./navigation";

export function MaterialCitation({ citation, currentMaterialId, onSeek }: {
  citation: Citation;
  currentMaterialId?: string;
  onSeek?(milliseconds: number): void;
}) {
  if (citation.youtube_url && typeof citation.start_ms === "number") {
    const label = `${citation.title ?? "Видео"} · ${time(citation.start_ms)} ↗`;
    if (currentMaterialId === citation.material_id && onSeek)
      return <button className="timestamp" onClick={() => onSeek(citation.start_ms!)}>{label}</button>;
    return <a className="timestamp" href={citation.youtube_url} target="_blank" rel="noreferrer">{label}</a>;
  }
  return <a className="timestamp" href={routeHash({ section: "materials", materialId: citation.material_id, blockId: citation.block_id ?? citation.cue_id })}>
    {citation.title ?? "Открыть текст"} · к абзацу ↗
  </a>;
}
