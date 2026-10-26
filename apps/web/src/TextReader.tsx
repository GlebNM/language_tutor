import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { Cue, Material } from "./api";
import { Icon } from "./UI";

type ReadingPosition = { block_id: string | null; offset: number };

export function TextReader({ material, initialBlockId, renderWords }: {
  material: Material;
  initialBlockId?: string;
  renderWords(block: Cue): ReactNode;
}) {
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [bookmark, setBookmark] = useState<string | null>(initialBlockId ?? null);
  const article = useRef<HTMLDivElement>(null);
  const position = useRef<ReadingPosition | null>(null);
  const restoring = useRef(true);
  const searchRef = useRef(search);
  searchRef.current = search;
  useEffect(() => {
    const controller = new AbortController();
    restoring.current = true;
    let timer: number | undefined;
    function restore(blockId?: string | null) {
      if (controller.signal.aborted) return;
      const row = Array.from(article.current?.querySelectorAll<HTMLElement>("[data-paragraph]") ?? [])
        .find(element => element.dataset.paragraph === blockId);
      if (row && article.current) article.current.scrollTop = row.offsetTop - article.current.offsetTop;
      if (blockId) position.current = { block_id: blockId, offset: 0 };
      setBookmark(blockId ?? null);
      timer = window.setTimeout(() => { restoring.current = false; }, 100);
    }
    void api<ReadingPosition>(`/materials/${material.id}/reading-position`, { signal: controller.signal })
      .then(value => { position.current = value; restore(initialBlockId ?? value.block_id); })
      .catch(e => {
        if (controller.signal.aborted) return;
        setError(e.message);
        restore(initialBlockId);
      });
    async function save() {
      const value = position.current;
      if (!value?.block_id) return;
      try {
        await api(`/materials/${material.id}/reading-position`, { method: "PUT", body: JSON.stringify(value), keepalive: true });
        if (!controller.signal.aborted) setError("");
      } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
    }
    const saveTimer = window.setInterval(() => void save(), 5000);
    const onPageHide = () => void save();
    window.addEventListener("pagehide", onPageHide);
    return () => {
      void save(); controller.abort(); window.clearTimeout(timer); window.clearInterval(saveTimer);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [material.id, initialBlockId]);
  function moved() {
    const list = article.current;
    if (!list || restoring.current || searchRef.current) return;
    const top = list.getBoundingClientRect().top;
    const rows = Array.from(list.querySelectorAll<HTMLElement>("[data-paragraph]"));
    const current = rows.find(row => row.getBoundingClientRect().bottom > top + 30);
    if (current?.dataset.paragraph) {
      position.current = { block_id: current.dataset.paragraph, offset: 0 };
      setBookmark(current.dataset.paragraph);
    }
  }
  const visible = material.cues.filter(block => block.text.toLowerCase().includes(search.toLowerCase()));
  return <div className="text-reader">
    <label className="search-field"><Icon name="search" size={16} /><input type="search" aria-label="Поиск в тексте" placeholder="Найти в тексте" value={search} onChange={event => setSearch(event.target.value)} /></label>
    {error && <p className="warning" role="alert">Место чтения не сохранено: {error}</p>}
    <div className="reading-paragraphs" ref={article} onScroll={moved} tabIndex={0} aria-label="Текст материала">
      {visible.map(block => <p key={block.id} data-paragraph={block.id} className={`reading-paragraph${bookmark === block.id ? " bookmarked" : ""}`}>{renderWords(block)}</p>)}
      {!visible.length && <p className="empty">Совпадений нет</p>}
    </div>
  </div>;
}
