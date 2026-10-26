import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { PlaybackSample } from "./listening";
import { createPlaybackTracker } from "./playbackProgress";
import type { PlaybackSpan } from "./playbackProgress";

type PendingSpan = PlaybackSpan & { id: string };
type Collection = {
  materialId: string;
  tracker: ReturnType<typeof createPlaybackTracker>;
  generation(): Promise<string>;
  pending: PendingSpan[];
  flight: Promise<void> | null;
  discarded: boolean;
};

export function usePlaybackProgress(materialId?: string) {
  const collection = useRef<Collection | null>(null);
  const outstanding = useRef(new Set<Collection>());
  const [error, setError] = useState("");
  const mounted = useRef(true);
  const send = useCallback((batch: Collection): Promise<void> => {
    if (batch.discarded) {
      outstanding.current.delete(batch);
      return Promise.resolve();
    }
    batch.pending.push(...batch.tracker.drain().map(span => ({ ...span, id: crypto.randomUUID() })));
    if (batch.flight) return batch.flight;
    const task = (async () => {
      if (!batch.pending.length) { outstanding.current.delete(batch); return; }
      const generation = await batch.generation();
      if (batch.discarded) { outstanding.current.delete(batch); return; }
      while (batch.pending.length) {
        const intervals = batch.pending.slice(0, 100);
        await api("/progress/playback", {
          method: "POST", keepalive: true,
          body: JSON.stringify({ material_id: batch.materialId, generation, intervals }),
        });
        batch.pending.splice(0, intervals.length);
      }
      outstanding.current.delete(batch);
      if (mounted.current && !batch.discarded) setError("");
    })();
    batch.flight = task;
    void task.then(() => { batch.flight = null; }, e => {
      batch.flight = null;
      if (mounted.current && !batch.discarded) setError(e.message);
    });
    return task;
  }, []);
  const flush = useCallback(async () => {
    if (collection.current) outstanding.current.add(collection.current);
    await Promise.all([...outstanding.current].map(send));
  }, [send]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (!materialId) { collection.current = null; return; }
    let generation: string | undefined;
    let generationRequest: Promise<string> | null = null;
    const batch: Collection = {
      materialId, tracker: createPlaybackTracker(Date.now() - performance.now()),
      generation() {
        if (generation) return Promise.resolve(generation);
        if (!generationRequest) {
          generationRequest = api<{ storage_generation: string }>("/profile")
            .then(value => { generation = value.storage_generation; return generation; })
            .finally(() => { generationRequest = null; });
        }
        return generationRequest;
      },
      pending: [], flight: null, discarded: false,
    };
    void batch.generation().catch(() => {});
    collection.current = batch;
    const save = () => {
      outstanding.current.add(batch);
      void send(batch).catch(() => {});
    };
    const timer = window.setInterval(save, 15_000);
    window.addEventListener("pagehide", save);
    window.addEventListener("online", save);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("pagehide", save);
      window.removeEventListener("online", save);
      save();
      if (collection.current === batch) collection.current = null;
    };
  }, [materialId, send]);
  return {
    sample(value: PlaybackSample) { collection.current?.tracker.sample(value); },
    reset() {
      if (collection.current) collection.current.discarded = true;
      outstanding.current.forEach(batch => { batch.discarded = true; });
      collection.current = null;
      outstanding.current.clear();
      setError("");
    },
    flush, error,
  };
}
