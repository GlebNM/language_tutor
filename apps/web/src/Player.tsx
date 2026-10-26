import { Icon } from "./UI";
import { learnerStorageKey } from "./session";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import type { PlaybackSample } from "./listening";
import { createCuePlayback, playbackShortcut } from "./cuePlayback";
import type { PlaybackCue, PlaybackShortcut } from "./cuePlayback";
import "./playback.css";

type YouTubePlayer = {
  getCurrentTime(): number;
  getPlayerState(): number;
  getPlaybackRate(): number;
  getAvailablePlaybackRates(): number[];
  setPlaybackRate(rate: number): void;
  getVolume(): number;
  isMuted(): boolean;
  pauseVideo(): void;
  playVideo(): void;
  cueVideoById(options: { videoId: string; startSeconds: number }): void;
  seekTo(seconds: number, allowSeekAhead: boolean): void;
  destroy(): void;
};
type YouTubeAPI = {
  Player: new (
    element: HTMLElement,
    options: {
      videoId: string;
      host: string;
      playerVars: Record<string, string | number>;
      events: {
        onReady(): void;
        onError(event: { data: number }): void;
        onStateChange(event: { data: number }): void;
        onPlaybackRateChange(event: { data: number }): void;
      };
    },
  ) => YouTubePlayer;
};
declare global {
  interface Window {
    YT?: YouTubeAPI;
    onYouTubeIframeAPIReady?: () => void;
  }
}
let apiPromise: Promise<YouTubeAPI> | undefined;
function loadYouTube(): Promise<YouTubeAPI> {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (!apiPromise) {
    apiPromise = new Promise<YouTubeAPI>((resolve, reject) => {
      const timeout = window.setTimeout(
        () =>
          reject(
            new Error(
              "Не удалось загрузить YouTube. Проверьте интернет и доступ к видео.",
            ),
          ),
        15000,
      );
      window.onYouTubeIframeAPIReady = () => {
        window.clearTimeout(timeout);
        if (window.YT) resolve(window.YT);
      };
      const script = document.createElement("script");
      script.src = "https://www.youtube.com/iframe_api";
      script.onerror = () => {
        window.clearTimeout(timeout);
        reject(new Error("YouTube недоступен. Субтитры доступны для чтения."));
      };
      document.head.append(script);
    }).catch((error) => {
      apiPromise = undefined;
      throw error;
    });
  }
  return apiPromise;
}
export type PlayerHandle = {
  pause(): void;
  play(): void;
  seek(milliseconds: number): void;
};
export const Player = forwardRef<
  PlayerHandle,
  {
    videoId: string;
    cues: readonly PlaybackCue[];
    onTime(milliseconds: number): void;
    onPlayback?(sample: PlaybackSample): void;
    onEnded?(): void;
  }
>(function Player({ videoId, cues, onTime, onPlayback, onEnded }, ref) {
  const container = useRef<HTMLDivElement>(null);
  const player = useRef<YouTubePlayer | null>(null);
  const initialized = useRef(false);
  const pauseRequested = useRef(false);
  const playRequested = useRef(false);
  const pendingSeek = useRef<number | null>(null);
  const cuedPosition = useRef<number | null>(null);
  const heldCaption = useRef<number | null>(null);
  const heldAt = useRef<number | null>(null);
  const desiredRate = useRef(1);
  const pendingRate = useRef<number | null>(null);
  const timingKey = useMemo(
    () =>
      cues.map((cue) => `${cue.id}:${cue.start_ms}:${cue.end_ms}`).join("|"),
    [cues],
  );
  const playback = useMemo(() => createCuePlayback(cues), [videoId, timingKey]);
  const [position, setPosition] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [rates, setRates] = useState([1]);
  const [looping, setLooping] = useState(false);
  const [autoPause, setAutoPause] = useState(false);
  const onTimeRef = useRef(onTime);
  onTimeRef.current = onTime;
  const onPlaybackRef = useRef(onPlayback);
  onPlaybackRef.current = onPlayback;
  const onEndedRef = useRef(onEnded);
  onEndedRef.current = onEnded;
  function sample(
    instance: YouTubePlayer,
    discontinuity = false,
    position?: number,
  ) {
    const value: PlaybackSample = {
      position: position ?? instance.getCurrentTime() * 1000,
      clock: performance.now(),
      playing: instance.getPlayerState() === 1,
      audible: !instance.isMuted() && instance.getVolume() > 0,
      rate: instance.getPlaybackRate(),
      discontinuity,
    };
    onPlaybackRef.current?.(value);
    return playback.observe(value);
  }
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [retry, setRetry] = useState(0);
  function pause() {
    pauseRequested.current = true;
    playRequested.current = false;
    if (initialized.current && player.current) {
      sample(player.current);
      player.current.pauseVideo();
    }
    setPlaying(false);
  }
  function play() {
    pauseRequested.current = false;
    heldCaption.current = null;
    playRequested.current = true;
    const instance = player.current;
    if (instance && initialized.current) instance.playVideo();
  }
  function seek(ms: number, keepLoop = false) {
    if (!keepLoop) {
      playback.cancelLoop();
      setLooping(false);
    }
    heldCaption.current = null;
    const instance = player.current;
    if (!instance || !initialized.current) {
      pendingSeek.current = ms;
      onTimeRef.current(ms);
      setPosition(ms);
      return;
    }
    sample(instance);
    const state = instance.getPlayerState();
    const keepPaused = pauseRequested.current || ![1, 3].includes(state);
    if (keepPaused && [-1, 0, 5].includes(state)) {
      // cueVideoById avoids the implicit play caused by seekTo before playback.
      cuedPosition.current = ms;
      pendingRate.current = desiredRate.current;
      instance.cueVideoById({ videoId, startSeconds: ms / 1000 });
    } else {
      cuedPosition.current = null;
      instance.seekTo(ms / 1000, true);
      if (keepPaused) instance.pauseVideo();
    }
    onTimeRef.current(ms);
    setPosition(ms);
    sample(instance, true, ms);
  }
  function update(instance: YouTubePlayer) {
    const rawPosition =
      cuedPosition.current ?? instance.getCurrentTime() * 1000;
    // Both learning trackers must see the completed interval before a loop seek.
    const action = sample(instance, false, rawPosition);
    setPlaying(
      [1, 3].includes(instance.getPlayerState()) && !pauseRequested.current,
    );
    setLooping(!!playback.loop);
    if (heldAt.current !== null && Math.abs(rawPosition - heldAt.current) > 150)
      heldCaption.current = null;
    const display = heldCaption.current ?? rawPosition;
    setPosition(display);
    onTimeRef.current(display);
    if (!pauseRequested.current && action?.kind === "loop") {
      seek(action.position, true);
      play();
      return true;
    }
    if (!pauseRequested.current && action?.kind === "pause") {
      heldCaption.current = action.position;
      heldAt.current = rawPosition;
      pause();
      setPosition(action.position);
      onTimeRef.current(action.position);
    }
    return false;
  }
  function applyRate(instance: YouTubePlayer, preferred: number) {
    const available = instance.getAvailablePlaybackRates();
    const supported = available.length ? available : [1];
    setRates(supported);
    desiredRate.current = supported.includes(preferred) ? preferred : 1;
    pendingRate.current = desiredRate.current;
    instance.setPlaybackRate(desiredRate.current);
    const actual = instance.getPlaybackRate();
    setRate(actual);
    if (actual === desiredRate.current) pendingRate.current = null;
  }
  function command(action: PlaybackShortcut) {
    const instance = player.current;
    if (!instance || !initialized.current) return;
    const at =
      heldCaption.current ??
      cuedPosition.current ??
      instance.getCurrentTime() * 1000;
    if (action === "toggle") {
      if ([1, 3].includes(instance.getPlayerState()) || playRequested.current)
        pause();
      else play();
    } else if (action === "autoPause") {
      playback.setAutoPause(!playback.autoPause);
      setAutoPause(playback.autoPause);
      setLooping(!!playback.loop);
    } else if (action === "loop") {
      const cue = playback.toggleLoop(at);
      setLooping(!!cue);
      setAutoPause(playback.autoPause);
      if (cue) {
        seek(cue.start_ms, true);
        play();
      }
    } else {
      const cue = playback.target(at, action);
      if (cue) {
        seek(cue.start_ms);
        play();
      }
    }
  }
  const onShortcut = useRef<(event: KeyboardEvent) => void>(() => {});
  onShortcut.current = (event) => {
    if (
      event.defaultPrevented ||
      event.repeat ||
      event.isComposing ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey
    )
      return;
    const target = event.target;
    if (
      target instanceof Element &&
      target.closest(
        'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"]',
      )
    )
      return;
    if (document.querySelector('[role="dialog"], dialog[open]')) return;
    if (
      event.code === "Space" &&
      target instanceof Element &&
      target.closest("button, a, summary")
    )
      return;
    const action = playbackShortcut(event.code);
    if (!action || !initialized.current) return;
    event.preventDefault();
    command(action);
  };
  useEffect(() => {
    const handle = (event: KeyboardEvent) => onShortcut.current(event);
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, []);
  useImperativeHandle(
    ref,
    () => ({
      pause,
      play,
      seek,
    }),
    [videoId, playback],
  );
  useEffect(() => {
    let active = true;
    let interval: number | undefined;
    let readyTimeout: number | undefined;
    pauseRequested.current = false;
    initialized.current = false;
    playRequested.current = false;
    pendingSeek.current = null;
    cuedPosition.current = null;
    heldCaption.current = null;
    setLooping(false);
    setAutoPause(false);
    playback.cancelLoop();
    playback.setAutoPause(false);
    setReady(false);
    setError("");
    onTimeRef.current(0);
    const mount = document.createElement("div");
    container.current?.replaceChildren(mount);
    loadYouTube()
      .then((yt) => {
        if (!active) return;
        readyTimeout = window.setTimeout(() => {
          if (active)
            setError(
              "Плеер не ответил. Проверьте доступ к YouTube; материал можно читать ниже.",
            );
        }, 15000);
        const instance = new yt.Player(mount, {
          videoId,
          host: "https://www.youtube.com",
          playerVars: {
            origin: window.location.origin,
            playsinline: 1,
            rel: 0,
            enablejsapi: 1,
          },
          events: {
            onReady() {
              if (!active) return;
              initialized.current = true;
              window.clearTimeout(readyTimeout);
              setReady(true);
              setError("");
              let preferred = desiredRate.current;
              try {
                preferred =
                  Number(
                    localStorage.getItem(learnerStorageKey("playback-rate")),
                  ) || 1;
              } catch {
                /* Browser storage is optional. */
              }
              applyRate(instance, preferred);
              const queuedSeek = pendingSeek.current;
              pendingSeek.current = null;
              if (queuedSeek !== null) {
                cuedPosition.current = queuedSeek;
                pendingRate.current = desiredRate.current;
                instance.cueVideoById({
                  videoId,
                  startSeconds: queuedSeek / 1000,
                });
              }
              if (playRequested.current) {
                instance.playVideo();
              } else if (pauseRequested.current) {
                instance.pauseVideo();
              }
              interval = window.setInterval(() => {
                if (active) {
                  update(instance);
                }
              }, 100);
              sample(instance);
            },
            onStateChange(event) {
              if (!active) return;
              if (event.data === 1) {
                // Native player controls count as the learner explicitly resuming.
                pauseRequested.current = false;
                playRequested.current = false;
                cuedPosition.current = null;
                heldCaption.current = null;
              } else if (event.data === 2) {
                pauseRequested.current = true;
                playRequested.current = false;
              }
              if (
                event.data === 5 ||
                (event.data === 1 && pendingRate.current !== null)
              )
                applyRate(instance, desiredRate.current);
              if (event.data === 5 && playRequested.current)
                instance.playVideo();
              const restarted = update(instance);
              if (event.data === 0 && !restarted) onEndedRef.current?.();
            },
            onPlaybackRateChange(event) {
              if (!active) return;
              setRate(event.data);
              if (
                pendingRate.current !== null &&
                pendingRate.current !== event.data
              )
                return;
              pendingRate.current = null;
              desiredRate.current = event.data;
              try {
                localStorage.setItem(
                  learnerStorageKey("playback-rate"),
                  String(event.data),
                );
              } catch {
                /* Playback remains available without storage. */
              }
              update(instance);
            },
            onError(event) {
              if (!active) return;
              initialized.current = false;
              window.clearTimeout(readyTimeout);
              setReady(false);
              setError(
                [101, 150].includes(event.data)
                  ? "Владелец запретил встраивание этого видео. Субтитры сохранены и доступны для чтения."
                  : event.data === 100
                    ? "Видео удалено или недоступно. Субтитры доступны для чтения."
                    : event.data === 153
                      ? "YouTube не разрешил воспроизведение в этом окружении (153). Откройте видео на YouTube."
                      : `Не удалось воспроизвести видео (${event.data}). Проверьте интернет и доступ к YouTube.`,
              );
            },
          },
        });
        player.current = instance;
      })
      .catch((reason) => {
        if (active)
          setError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => {
      active = false;
      initialized.current = false;
      window.clearInterval(interval);
      window.clearTimeout(readyTimeout);
      pendingSeek.current = null;
      playRequested.current = false;
      player.current?.destroy();
      player.current = null;
    };
  }, [videoId, retry, playback]);
  return (
    <div className="player-area">
      <div className="player-container" ref={container} />
      <div
        className="playback-controls"
        role="group"
        aria-label="Управление просмотром"
      >
        <div className="playback-buttons">
          <button
            className="secondary"
            disabled={!ready || !playback.target(position, "previous")}
            onClick={() => command("previous")}
            aria-label="Предыдущая реплика"
            title="Предыдущая реплика · A"
            aria-keyshortcuts="A"
          >
            <Icon name="back" size={14} />
            <span className="control-label">Назад</span>
          </button>
          <button
            className="secondary"
            disabled={!ready || !cues.length}
            onClick={() => command("repeat")}
            aria-label="Повторить реплику"
            title="Повторить реплику · S"
            aria-keyshortcuts="S"
          >
            <Icon name="repeat" size={14} />
            <span className="control-label">Повторить</span>
          </button>
          <button
            className="playback-toggle"
            disabled={!ready}
            onClick={() => command("toggle")}
            title="Воспроизведение / пауза · Пробел"
            aria-keyshortcuts="Space"
          >
            <Icon name={playing ? "pause" : "play"} size={14} />
            {playing ? "Пауза" : "Смотреть"}
          </button>
          <button
            className="secondary"
            disabled={!ready || !playback.target(position, "next")}
            onClick={() => command("next")}
            aria-label="Следующая реплика"
            title="Следующая реплика · D"
            aria-keyshortcuts="D"
          >
            <span className="control-label">Далее</span>
            <Icon name="next" size={14} />
          </button>
        </div>
        <div className="playback-options">
          <button
            className="secondary"
            disabled={!ready || !cues.length}
            aria-pressed={looping}
            aria-keyshortcuts="L"
            title="Повторять текущую реплику · L"
            onClick={() => command("loop")}
          >
            <Icon name="repeat" size={13} />
            Цикл
          </button>
          <button
            className="secondary"
            disabled={!ready || !cues.length}
            aria-pressed={autoPause}
            aria-keyshortcuts="Q"
            title="Пауза после каждой реплики · Q"
            onClick={() => command("autoPause")}
          >
            Автопауза
          </button>
          <label className="playback-speed">
            Скорость
            <select
              aria-label="Скорость воспроизведения"
              value={rate}
              disabled={!ready || rates.length < 2}
              onChange={(event) => {
                if (player.current)
                  applyRate(player.current, Number(event.target.value));
              }}
            >
              {[...new Set([...rates, rate])]
                .sort((a, b) => a - b)
                .map((value) => (
                  <option key={value} value={value}>
                    {value}×
                  </option>
                ))}
            </select>
          </label>
        </div>
        <details className="playback-help">
          <summary>Горячие клавиши</summary>
          <p>
            <kbd>A</kbd> назад · <kbd>S</kbd> ещё раз · <kbd>D</kbd> вперёд ·{" "}
            <kbd>Пробел</kbd> смотреть / пауза · <kbd>L</kbd> цикл ·{" "}
            <kbd>Q</kbd> автопауза.
          </p>
          <p>Вне полей ввода и окна YouTube.</p>
        </details>
      </div>
      {!ready && !error && (
        <p className="player-loading" role="status">
          Загружаем плеер YouTube…
        </p>
      )}
      {error && (
        <div className="player-error" role="alert">
          <p>{error}</p>
          <a
            href={`https://www.youtube.com/watch?v=${videoId}`}
            target="_blank"
            rel="noreferrer"
          >
            Открыть на YouTube ↗
          </a>
          <button className="quiet" onClick={() => setRetry((n) => n + 1)}>
            Повторить
          </button>
        </div>
      )}
    </div>
  );
});
