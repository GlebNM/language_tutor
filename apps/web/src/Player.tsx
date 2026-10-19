import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

type YouTubePlayer = {
  getCurrentTime(): number;
  getPlayerState(): number;
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
  { videoId: string; onTime(milliseconds: number): void }
>(function Player({ videoId, onTime }, ref) {
  const container = useRef<HTMLDivElement>(null);
  const player = useRef<YouTubePlayer | null>(null);
  const initialized = useRef(false);
  const pauseRequested = useRef(false);
  const playRequested = useRef(false);
  const pendingSeek = useRef<number | null>(null);
  const cuedPosition = useRef<number | null>(null);
  const onTimeRef = useRef(onTime);
  onTimeRef.current = onTime;
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [retry, setRetry] = useState(0);
  useImperativeHandle(
    ref,
    () => ({
      pause: () => {
        pauseRequested.current = true;
        playRequested.current = false;
        if (initialized.current) player.current?.pauseVideo();
      },
      play: () => {
        pauseRequested.current = false;
        const instance = player.current;
        if (instance && initialized.current) instance.playVideo();
        else playRequested.current = true;
      },
      seek: (ms) => {
        const instance = player.current;
        if (!instance || !initialized.current) {
          pendingSeek.current = ms;
          onTimeRef.current(ms);
          return;
        }
        const state = instance.getPlayerState();
        const keepPaused = pauseRequested.current || ![1, 3].includes(state);
        if (keepPaused && [-1, 0, 5].includes(state)) {
          // seekTo starts an unstarted/cued/ended video. Cue its new start instead.
          cuedPosition.current = ms;
          instance.cueVideoById({ videoId, startSeconds: ms / 1000 });
        } else {
          cuedPosition.current = null;
          instance.seekTo(ms / 1000, true);
          if (keepPaused) instance.pauseVideo();
        }
        onTimeRef.current(ms);
      },
    }),
    [videoId],
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
              const queuedSeek = pendingSeek.current;
              pendingSeek.current = null;
              if (queuedSeek !== null) {
                cuedPosition.current = queuedSeek;
                instance.cueVideoById({ videoId, startSeconds: queuedSeek / 1000 });
              }
              if (playRequested.current) {
                playRequested.current = false;
                instance.playVideo();
              } else if (pauseRequested.current) {
                instance.pauseVideo();
              }
              interval = window.setInterval(() => {
                if (active)
                  onTimeRef.current(
                    cuedPosition.current ?? instance.getCurrentTime() * 1000,
                  );
              }, 100);
            },
            onStateChange(event) {
              if (!active) return;
              if (event.data === 1) {
                // Native player controls count as the learner explicitly resuming.
                pauseRequested.current = false;
                playRequested.current = false;
                cuedPosition.current = null;
              }
              onTimeRef.current(
                cuedPosition.current ?? instance.getCurrentTime() * 1000,
              );
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
  }, [videoId, retry]);
  return (
    <div className="player-area">
      <div className="player-container" ref={container} />
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
