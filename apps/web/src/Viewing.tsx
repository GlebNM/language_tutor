import { useEffect, useRef, useState } from "react";
import { api, isTimedCue } from "./api";
import type { Cue, Material, Selection, Token } from "./api";
import { createListeningTracker } from "./listening";
import type { PlaybackSample } from "./listening";
import { LevelScale } from "./KnowledgeControl";
import "./viewing.css";

type Entry = {
  lexeme_id: string;
  lemma: string;
  part_of_speech: string;
  score: number;
  suggested_score: number;
  included: boolean;
};
type Summary = { viewing_id: string; entries: Entry[] };
type ViewingStart = {
  viewing_id: string | null;
  enabled: boolean;
  epoch: number;
};
type Collection = {
  id: string;
  tracker: ReturnType<typeof createListeningTracker>;
  pending: Set<string>;
  flight: Promise<void> | null;
  clicks: Map<string, () => Promise<Selection>>;
};

export function LearningSettings() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void api<ViewingStart>("/settings/learning", { signal: controller.signal })
      .then((value) => setEnabled(value.enabled))
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, []);
  async function change() {
    if (enabled == null || busy) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<ViewingStart>("/settings/learning", {
        method: "PUT",
        body: JSON.stringify({ enabled: !enabled }),
      });
      setEnabled(value.enabled);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="settings-card learning-settings">
      <label className="learning-setting">
        <span>
          <strong>Автоматически пополнять словарь</strong>
          <span className="setting-description">
            Прослушанные слова без отметки станут знакомыми, нажатые в субтитрах
            — незнакомыми. В конце вы проверите и сохраните сводку.
          </span>
        </span>
        <input
          type="checkbox"
          role="switch"
          className="setting-switch"
          checked={enabled ?? false}
          disabled={enabled == null || busy}
          onChange={() => void change()}
        />
      </label>
      <p className="help">
        Выключение сбрасывает неподтверждённые результаты просмотров. Слова, уже
        сохранённые в словарь, остаются.
      </p>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function useViewing(
  material: Material | null,
  onSaved: () => Promise<void>,
) {
  const collection = useRef<Collection | null>(null);
  const materialId = useRef(material?.id);
  materialId.current = material?.id;
  const readiness = useRef<Promise<Collection | null>>(Promise.resolve(null));
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const action = useRef(false);
  const reviewRequest = useRef(0);
  const cues = useRef(material?.cues ?? []);
  cues.current = material?.cues ?? [];

  function flush(batch: Collection): Promise<void> {
    if (batch.flight) return batch.flight;
    const task = (async () => {
      while (batch.pending.size) {
        const cueIds = [...batch.pending].slice(0, 100);
        await api(`/viewings/${batch.id}/cues`, {
          method: "POST",
          body: JSON.stringify({ cue_ids: cueIds }),
          keepalive: true,
        });
        cueIds.forEach((id) => batch.pending.delete(id));
      }
    })();
    batch.flight = task;
    void task.then(
      () => {
        batch.flight = null;
      },
      () => {
        batch.flight = null;
      },
    );
    return task;
  }

  useEffect(() => {
    let active = true;
    const materialId = material?.id;
    collection.current = null;
    ++reviewRequest.current;
    setEnabled(false);
    setOpen(false);
    setSummary(null);
    setError("");
    setNotice("");
    action.current = false;
    setBusy(false);
    setSaving(false);
    async function resume() {
      if (!materialId) return null;
      try {
        const value = await api<ViewingStart>(
          `/materials/${materialId}/viewing`,
          { method: "POST", body: "{}" },
        );
        if (!active) return null;
        setEnabled(value.enabled);
        if (!value.viewing_id) {
          collection.current = null;
          ++reviewRequest.current;
          setOpen(false);
          setSummary(null);
          return null;
        }
        if (collection.current?.id !== value.viewing_id) {
          ++reviewRequest.current;
          setOpen(false);
          setSummary(null);
          collection.current = {
            id: value.viewing_id,
            tracker: createListeningTracker(cues.current.filter(isTimedCue)),
            pending: new Set(),
            flight: null,
            clicks: new Map(),
          };
        }
        return collection.current;
      } catch (e) {
        if (active) setError((e as Error).message);
        return null;
      }
    }
    readiness.current = resume();
    const focus = () => {
      readiness.current = resume();
    };
    window.addEventListener("focus", focus);
    return () => {
      active = false;
      window.removeEventListener("focus", focus);
      const batch = collection.current;
      if (batch) void flush(batch).catch(() => {});
      collection.current = null;
    };
  }, [material?.id]);

  function sample(value: PlaybackSample) {
    const batch = collection.current;
    if (!batch) return;
    const completed = batch.tracker.sample(value);
    completed.forEach((id) => batch.pending.add(id));
    if (completed.length)
      void flush(batch).catch((e) => {
        if (collection.current === batch) setError(e.message);
      });
  }

  async function select(cue: Cue, token: Token): Promise<Selection> {
    const materialId = material?.id;
    const batch = await readiness.current;
    const requestId = crypto.randomUUID();
    const payload = {
      material_id: materialId,
      cue_id: cue.id,
      token_id: token.id,
      request_id: requestId,
      ...(batch ? { viewing_id: batch.id } : {}),
    };
    let running: Promise<Selection> | null = null;
    const send = (): Promise<Selection> => {
      if (running) return running;
      const task = api<Selection>("/activity/word-selections", {
        method: "POST",
        body: JSON.stringify(payload),
        keepalive: true,
      });
      running = task;
      void task.then(
        () => {
          batch?.clicks.delete(requestId);
          running = null;
        },
        () => {
          running = null;
        },
      );
      return task;
    };
    batch?.clicks.set(requestId, send);
    return send();
  }

  async function finish() {
    const batch = await readiness.current;
    if (!batch || collection.current !== batch) return;
    const request = ++reviewRequest.current;
    setOpen(true);
    setSummary(null);
    setError("");
    try {
      await Promise.all([
        flush(batch),
        ...[...batch.clicks.values()].map((send) => send()),
      ]);
      const value = await api<Summary>(`/viewings/${batch.id}`);
      if (collection.current === batch && request === reviewRequest.current)
        setSummary(value);
    } catch (e) {
      if (collection.current === batch && request === reviewRequest.current)
        setError((e as Error).message);
    }
  }

  async function choose(
    entry: Entry,
    change: Partial<Pick<Entry, "score" | "included">>,
  ) {
    const batch = collection.current;
    if (!batch || action.current) return;
    action.current = true;
    setBusy(true);
    setError("");
    try {
      const value = await api<Summary>(`/viewings/${batch.id}/choice`, {
        method: "PUT",
        body: JSON.stringify({
          lexeme_id: entry.lexeme_id,
          score: entry.score,
          included: entry.included,
          ...change,
        }),
      });
      if (collection.current === batch) setSummary(value);
    } catch (e) {
      if (collection.current === batch) setError((e as Error).message);
    } finally {
      action.current = false;
      setBusy(false);
    }
  }

  async function save() {
    const batch = collection.current;
    const savedMaterial = material?.id;
    if (!batch || action.current || !summary) return;
    action.current = true;
    setBusy(true);
    setSaving(true);
    setError("");
    try {
      const expected = summary.entries.filter((entry) => entry.included).length;
      const value = await api<{ saved: number }>(
        `/viewings/${batch.id}/confirm`,
        { method: "POST", body: "{}" },
      );
      if (collection.current !== batch) return;
      ++reviewRequest.current;
      setOpen(false);
      setSummary(null);
      setNotice(
        `Сохранено в словарь: ${value.saved}.${value.saved < expected ? " Уже оценённые слова пропущены." : ""}`,
      );
      collection.current = null;
      // Further listening is a new batch, with all confirmed words protected.
      readiness.current = api<ViewingStart>(
        `/materials/${savedMaterial}/viewing`,
        { method: "POST", body: "{}" },
      ).then((start) => {
        if (materialId.current !== savedMaterial) return null;
        setEnabled(start.enabled);
        if (start.viewing_id)
          collection.current = {
            id: start.viewing_id,
            tracker: createListeningTracker(cues.current.filter(isTimedCue)),
            pending: new Set(),
            flight: null,
            clicks: new Map(),
          };
        return collection.current;
      });
      await Promise.all([onSavedRef.current(), readiness.current]);
    } catch (e) {
      if (materialId.current === savedMaterial) setError((e as Error).message);
    } finally {
      if (materialId.current === savedMaterial) {
        action.current = false;
        setBusy(false);
        setSaving(false);
      }
    }
  }

  function close() {
    if (saving) return;
    ++reviewRequest.current;
    setOpen(false);
    setSummary(null);
  }
  return {
    enabled,
    error,
    notice,
    open,
    summary,
    busy,
    saving,
    sample,
    select,
    finish,
    choose,
    save,
    close,
  };
}

export function ViewingReview({
  viewing,
}: {
  viewing: ReturnType<typeof useViewing>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (viewing.open && !dialog.current?.open) dialog.current?.showModal();
    else if (!viewing.open && dialog.current?.open) dialog.current.close();
  }, [viewing.open]);
  const included =
    viewing.summary?.entries.filter((entry) => entry.included) ?? [];
  return (
    <dialog
      ref={dialog}
      className="viewing-dialog"
      aria-labelledby="viewing-title"
      onCancel={(event) => {
        event.preventDefault();
        viewing.close();
      }}
    >
      <div className="viewing-review-heading">
        <div>
          <h2 id="viewing-title">Итоги просмотра</h2>
        </div>
        <button
          className="quiet"
          disabled={viewing.saving}
          onClick={viewing.close}
          aria-label="Закрыть сводку"
        >
          ×
        </button>
      </div>
      <p className="help">Оценки применятся после сохранения сводки.</p>
      {viewing.error && (
        <p className="warning" role="alert">
          {viewing.error}
          <button
            className="quiet"
            disabled={viewing.busy}
            onClick={() => void viewing.finish()}
          >
            Обновить сводку
          </button>
        </p>
      )}
      {!viewing.summary ? (
        !viewing.error && <p role="status">Готовим сводку…</p>
      ) : (
        <>
          <p className="viewing-count" role="status">
            К добавлению: <strong>{included.length}</strong>
            {included.length > 0 &&
              ` · знакомых: ${included.filter((entry) => entry.score >= 2).length} · незнакомых: ${included.filter((entry) => entry.score < 2).length}`}
          </p>
          {viewing.summary.entries.length ? (
            <div className="viewing-words">
              <table>
                <thead>
                  <tr>
                    <th>Добавить</th>
                    <th>Слово</th>
                    <th>Знакомость</th>
                  </tr>
                </thead>
                <tbody>
                  {viewing.summary.entries.map((entry) => (
                    <tr
                      key={entry.lexeme_id}
                      className={entry.included ? "" : "excluded"}
                    >
                      <td>
                        <input
                          type="checkbox"
                          checked={entry.included}
                          disabled={viewing.busy}
                          aria-label={`Добавить ${entry.lemma}`}
                          onChange={(event) =>
                            void viewing.choose(entry, {
                              included: event.target.checked,
                            })
                          }
                        />
                      </td>
                      <td>
                        <strong>{entry.lemma}</strong>
                        <small>{entry.part_of_speech}</small>
                      </td>
                      <td>
                        <LevelScale
                          label={`Знакомость ${entry.lemma}`}
                          value={entry.score}
                          disabled={viewing.busy || !entry.included}
                          onChange={(score) =>
                            void viewing.choose(entry, { score })
                          }
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty">Новых слов для добавления нет.</p>
          )}
        </>
      )}
      <div className="viewing-review-actions">
        <button
          className="secondary"
          disabled={viewing.saving}
          onClick={viewing.close}
        >
          Закрыть без сохранения
        </button>
        <button
          disabled={!viewing.summary || viewing.busy}
          onClick={() => void viewing.save()}
        >
          {viewing.saving ? "Сохраняем…" : "Сохранить в словарь"}
        </button>
      </div>
    </dialog>
  );
}
