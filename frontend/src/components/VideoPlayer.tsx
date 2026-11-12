import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, errorMessage } from "../api/client";
import { fetchStream } from "../api/videos";
import { requireSafeUrl, safeUrl } from "../lib/safeUrl";
import { createWatchTracker, viewThreshold } from "../lib/watchTracker";
import { Spinner } from "./Spinner";
import { StateMessage } from "./StateMessage";
import styles from "./VideoPlayer.module.css";

/** Повторов подряд без продвижения воспроизведения, после — сообщение и «Повторить». */
export const MAX_SOURCE_RETRIES = 3;
/** Продвижение после сбоя, при котором счётчик повторов сбрасывается (иначе — цикл на битом месте). */
const PROGRESS_RESET_SECONDS = 5;
/** MediaError.MEDIA_ERR_ABORTED: загрузку прервали (смена src) — не сбой источника. */
const MEDIA_ERR_ABORTED = 1;
/** Пауза перед 2-м и следующими повторами подряд (1-й — сразу: обычно просто истекла ссылка). */
export const RETRY_DELAY_MS = 1000;

interface Props {
  videoId: string;
  durationSeconds: number | null;
  /** Порог просмотра достигнут (один раз за жизнь плеера). */
  onWatched?: () => void;
}

interface Resume {
  time: number;
  play: boolean;
}

/**
 * Плеер на <video>. Ссылки на поток — presigned с коротким TTL (ADR 0004, день 4): когда ссылка
 * истекла (403 от хранилища) или источник упал, <video> даёт только событие error без кода HTTP.
 * Поэтому на любую ошибку источника плеер заново запрашивает /stream и продолжает с той же
 * позиции, а если видео играло — продолжает воспроизведение (решение архитектора, день 4).
 */
export function VideoPlayer({ videoId, durationSeconds, onWatched }: Props) {
  const stream = useQuery({
    queryKey: ["stream", videoId],
    queryFn: ({ signal }) => fetchStream(videoId, signal),
    // Ссылки не перезапрашиваются сами: смена src посреди просмотра сбросила бы буфер.
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const { refetch } = stream;

  const videoRef = useRef<HTMLVideoElement>(null);
  const resume = useRef<Resume | null>(null);
  const failures = useRef({ count: 0, at: 0 });
  const tracker = useRef(createWatchTracker());
  const watched = useRef(onWatched);
  useEffect(() => {
    watched.current = onWatched;
  }, [onWatched]);

  const [quality, setQuality] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [failed, setFailed] = useState(false);

  const sources = stream.data?.sources ?? [];
  const source = sources.find((s) => s.quality === quality) ?? sources[0];
  const src = safeUrl(source?.url);

  // src ставится здесь, а не атрибутом: после перезапроса ссылка может совпасть с прежней
  // (подпись в ту же секунду) — тогда источник перезагружается явно.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    if (video.getAttribute("src") !== src) video.src = requireSafeUrl(src);
    else if (reloadToken > 0) video.load();
  }, [src, reloadToken]);

  const capture = (video: HTMLVideoElement): Resume => ({
    // Позиция из прошлого сбоя, если загрузка не успела до неё дойти.
    time: Math.max(video.currentTime || 0, resume.current?.time ?? 0),
    play: resume.current?.play ?? !video.paused,
  });

  // Перезапрос в процессе: повторные события error (например, play на элементе в ошибке) не
  // запускают второй — он отменил бы первый и потратил попытку на старую ссылку (ревью дня 8).
  const reloading = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reloadSource = useCallback(
    async (delayMs = 0) => {
      reloading.current = true;
      try {
        if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
        if (!mounted.current) return;
        await refetch();
        if (mounted.current) setReloadToken((n) => n + 1);
      } finally {
        reloading.current = false;
      }
    },
    [refetch],
  );

  function handleError(event: SyntheticEvent<HTMLVideoElement>) {
    const video = event.currentTarget;
    if (!video.error || video.error.code === MEDIA_ERR_ABORTED || reloading.current) return;
    resume.current = capture(video);
    if (failures.current.count >= MAX_SOURCE_RETRIES) {
      setFailed(true);
      return;
    }
    const attempt = failures.current.count + 1;
    failures.current = { count: attempt, at: resume.current.time };
    void reloadSource((attempt - 1) * RETRY_DELAY_MS);
  }

  function handleLoadedMetadata(event: SyntheticEvent<HTMLVideoElement>) {
    const video = event.currentTarget;
    const pending = resume.current;
    if (!pending) return;
    resume.current = null;
    if (pending.time > 0) video.currentTime = pending.time;
    if (pending.play) video.play()?.catch(() => undefined);
  }

  function handleTimeUpdate(event: SyntheticEvent<HTMLVideoElement>) {
    const video = event.currentTarget;
    if (failures.current.count > 0 && video.currentTime >= failures.current.at + PROGRESS_RESET_SECONDS) {
      failures.current = { count: 0, at: 0 };
    }
    // Порог — в секундах видео (на скорости 2× набирается быстрее): накрутку клиентом всё равно
    // не остановить, ограничение — зона views в nginx (security.md, принятые риски).
    const duration = durationSeconds ?? video.duration;
    if (tracker.current.update(video.currentTime, viewThreshold(duration))) watched.current?.();
  }

  function changeQuality(next: string) {
    const video = videoRef.current;
    if (video) resume.current = capture(video);
    failures.current = { count: 0, at: 0 };
    setFailed(false);
    setQuality(next);
  }

  function retry() {
    failures.current = { count: 0, at: 0 };
    setFailed(false);
    void reloadSource();
  }

  if (stream.isPending) {
    return (
      <div className={styles.frame}>
        <Spinner />
      </div>
    );
  }
  // Ссылку отклонил safeUrl (не путь этого сайта) — не ставим её и сообщаем.
  if (stream.data && !src) {
    return (
      <div className={styles.frame}>
        <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={retry}>
          Источник видео недоступен.
        </StateMessage>
      </div>
    );
  }
  if (stream.isError && !stream.isFetching) {
    const status = stream.error instanceof ApiError ? stream.error.status : null;
    return (
      <div className={styles.frame}>
        {status === 404 ? (
          <StateMessage title="Видео недоступно" role="alert">
            Видео удалено, скрыто или доступ к нему закрыт.
          </StateMessage>
        ) : status === 409 ? (
          <StateMessage title="Видео ещё обрабатывается">Обновите страницу через минуту.</StateMessage>
        ) : (
          <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={retry}>
            {errorMessage(stream.error)}
          </StateMessage>
        )}
      </div>
    );
  }

  return (
    <div className={styles.player}>
      <div className={styles.frame}>
        <video
          ref={videoRef}
          className={styles.video}
          controls
          playsInline
          preload="metadata"
          poster={safeUrl(stream.data?.thumbnail_url)}
          data-testid="video"
          onError={handleError}
          onLoadedMetadata={handleLoadedMetadata}
          onTimeUpdate={handleTimeUpdate}
          onSeeking={() => tracker.current.seek()}
        />
        {failed && (
          <div className={styles.overlay}>
            <StateMessage title="Не удалось воспроизвести видео" role="alert" onRetry={retry}>
              Проверьте подключение и повторите.
            </StateMessage>
          </div>
        )}
      </div>
      {sources.length > 1 && (
        <label className={styles.quality}>
          Качество{" "}
          <select value={source?.quality} onChange={(e) => changeQuality(e.target.value)}>
            {sources.map((s) => (
              <option key={s.quality} value={s.quality}>
                {s.quality}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}
