import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

const paths = {
  library: "M4 4h6v16H4zM14 4h6v16h-6zM4 8h6M14 8h6",
  search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  words:
    "M3 5h7a3 3 0 0 1 3 3v13a4 4 0 0 0-4-2H3zM13 8a3 3 0 0 1 3-3h5v14h-5a4 4 0 0 0-3 2",
  chart: "M4 3v17h17M8 15l4-5 4 2 5-7",
  chat: "M21 11a9 9 0 0 1-9 9H4l-3 2 2-6a9 9 0 1 1 18-5M7 10h10M7 14h6",
  check: "M5 12l4 4L19 6",
  assessment: "M9 4h6M9 3h6v4H9zM9 5H5v16h14V5h-4M8 12h8M8 16h5",
  settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
  plus: "M12 5v14M5 12h14",
  close: "M6 6l12 12M18 6 6 18",
  back: "M15 5l-7 7 7 7",
  next: "M9 5l7 7-7 7",
  play: "M8 4l12 8-12 8z",
  pause: "M8 5v14M16 5v14",
  repeat: "M4 10a8 8 0 1 1 1 8M4 4v6h6",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  expand: "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5",
  reset: "M4 10a8 8 0 1 1 1 8M4 4v6h6",
  exclude:
    "M3 3l18 18M10 10a3 3 0 0 0 4 4M9 5c7-2 12 7 12 7s-1 2-3 4M6 6c-3 2-5 6-5 6s5 9 12 7",
} as const;
export type IconName = keyof typeof paths;
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function Modal({
  children,
  labelledBy,
  onClose,
  busy = false,
}: {
  children: ReactNode;
  labelledBy: string;
  onClose(): void;
  busy?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) {
        previous.focus({ preventScroll: true });
      }
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="modal"
      aria-labelledby={labelledBy}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(event) => {
        if (event.target === dialog.current && !busy) {
          const bounds = dialog.current.getBoundingClientRect();
          if (
            event.clientX < bounds.left ||
            event.clientX > bounds.right ||
            event.clientY < bounds.top ||
            event.clientY > bounds.bottom
          )
            onClose();
        }
      }}
    >
      {children}
    </dialog>
  );
}

export function Tabs<T extends string>({
  value,
  onChange,
  items,
  label,
}: {
  value: T;
  onChange(value: T): void;
  items: Array<{ id: T; label: string; count?: number }>;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={value === item.id}
          tabIndex={value === item.id ? 0 : -1}
          onClick={() => onChange(item.id)}
          onKeyDown={(event) => {
            const offset =
              event.key === "ArrowRight"
                ? 1
                : event.key === "ArrowLeft"
                  ? -1
                  : 0;
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? items.length - 1
                  : (index + offset + items.length) % items.length;
            if (!offset && event.key !== "Home" && event.key !== "End") return;
            event.preventDefault();
            onChange(items[next].id);
            (
              event.currentTarget.parentElement?.children[
                next
              ] as HTMLButtonElement
            )?.focus();
          }}
        >
          {item.label}
          {item.count !== undefined && (
            <span className="count">{item.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
