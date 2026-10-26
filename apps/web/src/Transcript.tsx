import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Cue } from "./api";
import { time, isTimedCue } from "./api";
import { Icon } from "./UI";

export function Transcript({
  cues,
  activeId,
  renderWords,
  onSeek,
}: {
  cues: Cue[];
  activeId?: string;
  renderWords(cue: Cue): ReactNode;
  onSeek(ms: number): void;
}) {
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const container = useRef<HTMLDivElement>(null);
  const activeRow = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const row = activeRow.current,
      list = container.current;
    if (!follow || search || !row || !list) return;
    // Scroll only the transcript, never the surrounding page or player.
    list.scrollTo({
      top: row.offsetTop - list.clientHeight / 3,
      behavior: "auto",
    });
  }, [activeId, follow, search]);
  const visible = cues.filter((cue) =>
    cue.text.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="transcript-panel">
      <div className="transcript-tools">
        <label className="search-field">
          <Icon name="search" size={16} />
          <input
            aria-label="Поиск в субтитрах"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Найти в субтитрах"
          />
        </label>
        <button
          className="quiet follow-caption"
          aria-pressed={follow && !search}
          onClick={() => {
            setSearch("");
            setFollow(true);
          }}
        >
          К текущей
        </button>
      </div>
      <div
        ref={container}
        className="transcript"
        tabIndex={0}
        aria-label="Текст субтитров"
        onWheel={() => setFollow(false)}
        onTouchMove={() => setFollow(false)}
        onKeyDown={(event) => {
          if (
            [
              "PageDown",
              "PageUp",
              "ArrowDown",
              "ArrowUp",
              "Home",
              "End",
            ].includes(event.key)
          )
            setFollow(false);
        }}
      >
        {visible.map((cue) => (
          <div
            ref={cue.id === activeId ? activeRow : undefined}
            className={`cue${cue.id === activeId ? " current" : ""}`}
            key={cue.id}
          >
            {isTimedCue(cue) && <button
              className="timestamp"
              onClick={() => onSeek(cue.start_ms)}
              aria-label={`Перейти к ${time(cue.start_ms)}`}
            >
              {time(cue.start_ms)}
            </button>}
            <p>{renderWords(cue)}</p>
          </div>
        ))}
        {!visible.length && <p className="empty">Совпадений нет</p>}
      </div>
    </div>
  );
}
