export type ReadingPosition = { block_id: string | null; offset: number };
const READING_LINE = 30;

export function captureReadingPosition(article: HTMLElement): ReadingPosition | null {
  const top = article.getBoundingClientRect().top;
  const row = Array.from(article.querySelectorAll<HTMLElement>("[data-paragraph]"))
    .find(element => element.getBoundingClientRect().bottom > top + READING_LINE);
  if (!row?.dataset.paragraph) return null;
  const words = Array.from(row.querySelectorAll<HTMLElement>("[data-reading-offset]"));
  const anchor = words.find(word => word.getBoundingClientRect().bottom > top + READING_LINE)
    ?? words.at(-1);
  return { block_id: row.dataset.paragraph, offset: Number(anchor?.dataset.readingOffset ?? 0) };
}

export function restoreReadingPosition(article: HTMLElement, position: ReadingPosition): void {
  const row = Array.from(article.querySelectorAll<HTMLElement>("[data-paragraph]"))
    .find(element => element.dataset.paragraph === position.block_id);
  if (!row) return;
  const words = Array.from(row.querySelectorAll<HTMLElement>("[data-reading-offset]"));
  const anchor = [...words].reverse().find(word => Number(word.dataset.readingOffset) <= position.offset)
    ?? words[0] ?? row;
  article.scrollTop += anchor.getBoundingClientRect().top
    - article.getBoundingClientRect().top - READING_LINE;
}
