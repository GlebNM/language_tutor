import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { Material } from "./api";
import { Modal } from "./UI";
import { readTextFile } from "./textMaterials";

export function TextImport({ material, onClose, onSaved }: {
  material?: Material;
  onClose(): void;
  onSaved(material: Material): void;
}) {
  const [title, setTitle] = useState(material?.title ?? "");
  const [content, setContent] = useState(material?.cues.map(block => block.text).join("\n\n") ?? "");
  const [source, setSource] = useState<"manual_text" | "text_file">("manual_text");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileRequest = useRef(0);
  async function loadFile(file?: File) {
    if (!file) return;
    const request = ++fileRequest.current;
    setBusy(true); setError("");
    try {
      const value = await readTextFile(file);
      if (request !== fileRequest.current) return;
      setContent(value.content);
      setTitle(current => current || value.title);
      setSource("text_file");
    } catch (e) { if (request === fileRequest.current) setError((e as Error).message); }
    finally { if (request === fileRequest.current) setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!content.trim()) { setError("Добавьте текст для чтения."); return; }
    setBusy(true); setError("");
    try {
      const value = await api<Material>(material ? `/materials/${material.id}/text` : "/materials/text", {
        method: material ? "PUT" : "POST", body: JSON.stringify({ title: title.trim(), content, source }),
      });
      onSaved(value);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <Modal labelledBy="text-import-title" onClose={onClose} busy={busy}>
    <div className="panel-title"><span /><button className="quiet close" disabled={busy} onClick={onClose} aria-label="Закрыть текст">×</button></div>
    <h2 id="text-import-title">{material ? "Редактировать текст" : "Добавить текст"}</h2>
    <form onSubmit={event => void save(event)}>
      <label>Название<input required maxLength={200} value={title} disabled={busy} onChange={event => setTitle(event.target.value)} /></label>
      <label>Текст<textarea className="text-import-content" required rows={12} value={content} disabled={busy} onChange={event => { setContent(event.target.value); setSource("manual_text"); }} /></label>
      <label className="file-field">Или загрузите TXT<input type="file" accept=".txt,text/plain" disabled={busy} onChange={event => void loadFile(event.target.files?.[0])} /><small>UTF-8 · до 200 000 символов</small></label>
      {error && <p className="warning" role="alert">{error}</p>}
      <button className="submit" disabled={busy}>{busy ? "Сохраняем…" : material ? "Сохранить новую версию" : "Добавить в библиотеку"}</button>
    </form>
  </Modal>;
}
