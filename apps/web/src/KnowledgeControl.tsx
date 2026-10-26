import { useEffect, useRef, useState } from "react";
import { Icon } from "./UI";

export const knowledgeNames = [
  "Не знаю",
  "Учу",
  "Узнаю в контексте",
  "Уверенно знаю",
];
const shortNames = ["Не знаю", "Учу", "Узнаю", "Знаю"];
const descriptions = [
  "Значение незнакомо",
  "Обычно нужна подсказка",
  "Понимаю в контексте",
  "Понимаю уверенно",
];
export type KnowledgeValue = { score: number | null; ignored: boolean };

export function LevelMark({ score }: { score: number | null }) {
  return (
    <span
      className={`level-symbol level-symbol-${score ?? "null"}`}
      aria-hidden="true"
    >
      {score === null ? (
        <span>–</span>
      ) : score === 3 ? (
        <Icon name="check" size={18} />
      ) : (
        <span className="level-bars">
          {[0, 1, 2].map((index) => (
            <i key={index} className={index < score ? "filled" : ""} />
          ))}
        </span>
      )}
    </span>
  );
}

export function LevelScale({
  value,
  onChange,
  disabled = false,
  label = "Знакомость",
}: {
  value: number | null;
  onChange(value: number): void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <div className="level-scale" role="group" aria-label={label}>
      {shortNames.map((name, score) => (
        <button
          type="button"
          key={score}
          className={`level-choice level-choice-${score}`}
          aria-label={`${score} · ${knowledgeNames[score]}`}
          aria-pressed={score === value}
          disabled={disabled}
          title={`${knowledgeNames[score]} — ${descriptions[score]}`}
          onClick={() => onChange(score)}
        >
          <LevelMark score={score} />
          <span>{name}</span>
        </button>
      ))}
    </div>
  );
}

export function KnowledgeControl({
  value,
  identity,
  provisional,
  onChange,
  label = "Знакомость",
  unsaved = false,
}: {
  value: KnowledgeValue;
  identity: string;
  provisional?: boolean;
  unsaved?: boolean;
  onChange(value: KnowledgeValue): Promise<void>;
  label?: string;
}) {
  const [current, setCurrent] = useState(value);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [failure, setFailure] = useState<{
    value: KnowledgeValue;
    message: string;
  } | null>(null);
  const pending = useRef(false);
  const generation = useRef(0);
  useEffect(() => {
    ++generation.current;
    pending.current = false;
    setBusy(false);
    setSaved(false);
    setFailure(null);
    setCurrent(value);
    return () => {
      ++generation.current;
    };
  }, [identity]);
  useEffect(() => {
    setCurrent({ score: value.score, ignored: value.ignored });
  }, [value.score, value.ignored]);
  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 1800);
    return () => clearTimeout(timer);
  }, [saved]);
  async function save(next: KnowledgeValue) {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setFailure(null);
    setSaved(false);
    const request = generation.current;
    try {
      await onChange(next);
      if (request === generation.current) {
        setCurrent(next);
        setSaved(true);
      }
    } catch (error) {
      if (request === generation.current)
        setFailure({ value: next, message: (error as Error).message });
    } finally {
      if (request === generation.current) {
        pending.current = false;
        setBusy(false);
      }
    }
  }
  return (
    <div className="knowledge-editor" aria-busy={busy}>
      <div className="knowledge-heading">
        <span>{label}</span>
        <span className="knowledge-feedback" role="status">
          {busy
            ? "Сохраняем…"
            : saved
              ? "Сохранено"
              : current.ignored
                ? "Исключено"
                : provisional
                  ? "Предварительно"
                  : current.score === null
                    ? "Без оценки"
                    : ""}
        </span>
      </div>
      <LevelScale
        value={current.ignored ? null : current.score}
        disabled={busy}
        label={label}
        onChange={(score) => void save({ score, ignored: false })}
      />
      <div className="knowledge-actions">
        {unsaved && current.score === null && (
          <button
            className="quiet"
            disabled={busy}
            onClick={() => void save({ score: null, ignored: false })}
          >
            Добавить без оценки
          </button>
        )}
        <details>
          <summary aria-label="Другие действия со знакомостью">
            <Icon name="more" size={16} /> Ещё
          </summary>
          <div className="knowledge-menu">
            <button
              type="button"
              className="quiet"
              disabled={busy}
              onClick={(event) => {
                event.currentTarget.closest("details")?.removeAttribute("open");
                void save({ score: null, ignored: false });
              }}
            >
              <Icon name="reset" size={15} />
              Сбросить оценку
            </button>
            <button
              type="button"
              className="quiet"
              disabled={busy}
              onClick={(event) => {
                event.currentTarget.closest("details")?.removeAttribute("open");
                void save({ ...current, ignored: !current.ignored });
              }}
            >
              <Icon name="exclude" size={15} />
              {current.ignored ? "Вернуть в изучение" : "Исключить из изучения"}
            </button>
          </div>
        </details>
      </div>
      {failure && (
        <div className="warning" role="alert">
          Не сохранено: {failure.message}{" "}
          <button
            type="button"
            className="quiet"
            onClick={() => void save(failure.value)}
          >
            Повторить
          </button>
        </div>
      )}
    </div>
  );
}
