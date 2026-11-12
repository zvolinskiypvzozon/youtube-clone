import { useCallback, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, errorMessage } from "../../api/client";
import { channelKey, fetchChannel } from "../../api/channels";
import type { VideoCard } from "../../api/types";
import { addView, fetchVideo, videoKey } from "../../api/videos";
import { useAuth } from "../../auth/AuthContext";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { SubscribeButton } from "../../components/SubscribeButton";
import { UserText } from "../../components/UserText";
import { initial } from "../../components/VideoCard";
import { VideoPlayer } from "../../components/VideoPlayer";
import { formatRelative, formatSubscribers, formatViews } from "../../lib/format";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { Comments } from "./Comments";
import { ReactionButtons } from "./ReactionButtons";
import { ReportForm } from "./ReportForm";
import { SaveToPlaylist } from "./SaveToPlaylist";
import { VideoStateBanner } from "./VideoStateBanner";
import styles from "./WatchPage.module.css";

/** Пока видео обрабатывается, владелец видит статус; карточка перезапрашивается. Брошенную
 * загрузку (uploading) не опрашиваем: она сама не завершится. */
export const PROCESSING_POLL_MS = 5000;

const isProcessing = (video: VideoCard | undefined) => video?.state?.status === "processing";

/** Описание длиннее трёх строк — показываем «Ещё» (точно измерить строки до отрисовки нельзя). */
const isLongText = (text: string) => text.length > 200 || text.split("\n").length > 3;

export function WatchPage() {
  // UUID из адреса — в нижнем регистре, как в ответах API: ключи кэша карточки совпадают с
  // ключами, которые обновляют реакции и комментарии (по video.id).
  const videoId = (useParams().videoId ?? "").toLowerCase();
  const video = useQuery({
    queryKey: videoKey(videoId),
    queryFn: ({ signal }) => fetchVideo(videoId, signal),
    refetchInterval: (query) => (isProcessing(query.state.data) ? PROCESSING_POLL_MS : false),
  });
  useDocumentTitle(video.data?.title ?? null);

  // Спиннер — только без данных: перезапрос карточки (смена сессии) не размонтирует плеер.
  if (video.isPending && !video.data) return <Spinner />;
  if (video.isError) {
    // 404 одинаков для несуществующего, приватного, скрытого и видео забаненного автора:
    // интерфейс не различает эти случаи (ADR 0006).
    if (video.error instanceof ApiError && video.error.status === 404) {
      return (
        <StateMessage title="Видео недоступно" role="alert">
          Видео не найдено, удалено или доступ к нему закрыт. <Link to="/">На главную</Link>
        </StateMessage>
      );
    }
    return (
      <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={() => void video.refetch()}>
        {errorMessage(video.error)}
      </StateMessage>
    );
  }
  // key: другое видео — новый плеер (счётчик просмотра и повторы — с нуля).
  return <WatchContent key={video.data.id} video={video.data} />;
}

function WatchContent({ video }: { video: VideoCard }) {
  const { user } = useAuth();
  const [expanded, setExpanded] = useState(false);
  const descriptionId = useId();
  const longDescription = isLongText(video.description);
  const channel = useQuery({
    queryKey: channelKey(video.channel.handle),
    queryFn: ({ signal }) => fetchChannel(video.channel.handle, signal),
  });
  const ready = video.state === null || video.state.status === "ready";
  const isOwner = Boolean(user && user.channel.id === video.channel.id);
  const channelPath = `/channel/${encodeURIComponent(video.channel.handle)}`;

  // Ошибка засчитывания просмотра пользователю не показывается.
  const onWatched = useCallback(() => {
    addView(video.id).catch(() => undefined);
  }, [video.id]);

  return (
    <article className={styles.page}>
      {ready ? (
        <VideoPlayer videoId={video.id} durationSeconds={video.duration_seconds} onWatched={onWatched} />
      ) : (
        <div className={styles.placeholder}>
          <StateMessage title={video.state?.status === "failed" ? "Видео не обработано" : "Видео обрабатывается"} />
        </div>
      )}

      <UserText text={video.title} as="h1" className={styles.title} lines={2} title />

      {video.state && <VideoStateBanner state={video.state} />}

      <div className={styles.bar}>
        <div className={styles.channel}>
          <Link to={channelPath} className={styles.avatar} aria-hidden="true" tabIndex={-1}>
            {initial(video.channel.title)}
          </Link>
          <div className={styles.channelText}>
            <Link to={channelPath} className={styles.channelName}>
              <UserText text={video.channel.title} lines={1} />
            </Link>
            {channel.data && (
              <span className={styles.muted}>{formatSubscribers(channel.data.subscribers_count)}</span>
            )}
          </div>
          {channel.data && <SubscribeButton channel={channel.data} />}
        </div>
        {ready && <ReactionButtons video={video} />}
      </div>

      <section className={styles.description} aria-label="Описание">
        <p className={styles.meta}>
          {formatViews(video.views_count)}
          {video.published_at && (
            <>
              {" · "}
              <time dateTime={video.published_at}>{formatRelative(video.published_at)}</time>
            </>
          )}
        </p>
        {video.description && (
          <>
            <UserText
              text={video.description}
              as="div"
              id={descriptionId}
              className={styles.preWrap}
              lines={expanded || !longDescription ? undefined : 3}
            />
            {longDescription && (
              <button
                type="button"
                className={styles.linkButton}
                aria-expanded={expanded}
                aria-controls={descriptionId}
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "Свернуть" : "Ещё"}
              </button>
            )}
          </>
        )}
      </section>

      {ready && !isOwner && video.state?.moderation !== "hidden" && (
        <div className={styles.secondary}>
          <ReportForm videoId={video.id} />
        </div>
      )}
      {isOwner && <SaveToPlaylist videoId={video.id} />}

      {ready && <Comments video={video} />}
    </article>
  );
}
