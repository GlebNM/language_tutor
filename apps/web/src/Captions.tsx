import { useEffect, useState } from "react";
import { api, time, isTimedCue } from "./api";
import type { Material } from "./api";

type Improvement = {
  status: "queued" | "running" | "completed" | "failed";
  completed_batches: number;
  total_batches: number;
  result_id: string | null;
  changed_cues: number;
  error: string | null;
  model: string;
  reasoning_effort: string | null;
};

export function Captions({ material, onOpen, onLibraryChanged }: {
  material: Material;
  onOpen(id: string): void;
  onLibraryChanged(): void;
}) {
  const [job, setJob] = useState<Improvement | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  useEffect(() => {
    if (material.source !== "youtube-automatic") return;
    const controller = new AbortController();
    let timer: number | undefined;
    const poll = async () => {
      try {
        const result = await api<Improvement | null>(`/materials/${material.id}/improvement`, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setJob(result);
        setError("");
        if (result?.status === "completed") onLibraryChanged();
        if (result?.status === "queued" || result?.status === "running") {
          timer = window.setTimeout(() => void poll(), 2000);
        }
      } catch (e) {
        if (!controller.signal.aborted) {
          setError((e as Error).message);
          timer = window.setTimeout(() => void poll(), 5000);
        }
      }
    };
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [material.id, material.source, starting]);

  async function start() {
    setStarting(true); setError("");
    try {
      setJob(await api<Improvement>(`/materials/${material.id}/improvement`, { method: "POST", body: "{}" }));
    } catch (e) { setError((e as Error).message); }
    finally { setStarting(false); }
  }

  if (material.source === "youtube-improved") {
    const changed = material.cues.filter(isTimedCue).filter(cue => cue.text !== cue.original_text);
    return <section className="caption-status">
      <p>Субтитры обработаны AI по тексту. Возможны ошибки; аудио не проверялось.</p>
      {material.previous_version_id && <button className="secondary" onClick={() => onOpen(material.previous_version_id!)}>Открыть исходную дорожку</button>}
      <details><summary>Сравнить с исходником · {changed.length} реплик</summary>
        {changed.map(cue => <div key={cue.id} className="caption-change">
          <strong>{time(cue.start_ms)}</strong><p>Было: {cue.original_text}</p><p>Стало: {cue.text}</p>
        </div>)}
      </details>
    </section>;
  }
  if (material.source !== "youtube-automatic") return null;
  const busy = starting || job?.status === "queued" || job?.status === "running";
  return <section className="caption-status" aria-label="Улучшение субтитров">
    <p>Автоматические субтитры могут содержать ошибки. AI улучшает текст, сохраняя тайминги и исходную дорожку.</p>
    {busy && <div role="status">
      <p>{job?.status === "queued" ? "Ожидает обработки…" : "Улучшаем субтитры…"} {job ? `${job.completed_batches} из ${job.total_batches} частей` : ""}</p>
      <progress max={job?.total_batches ?? 1} value={job?.completed_batches ?? 0} />
      <p className="help">Можно смотреть видео или вернуться позже.</p>
    </div>}
    {job?.status === "completed" && <div role="status">
      <p>{job.changed_cues ? `Обработано: ${job.changed_cues} реплик.` : "Проверка завершена. Уверенных исправлений не найдено."} · {job.model} · {job.reasoning_effort ?? "по умолчанию"}</p>
      {job.result_id && job.result_id !== material.id && <button onClick={() => onOpen(job.result_id!)}>Открыть улучшенную версию</button>}
    </div>}
    {job?.status === "failed" && <p className="warning" role="alert">{job.error}</p>}
    {error && <p className="warning" role="alert">{error}</p>}
    {!busy && job?.status !== "completed" && <button className="secondary" onClick={() => void start()}>
      {job?.status === "failed" ? "Повторить улучшение" : "Улучшить субтитры с AI"}
    </button>}
  </section>;
}
