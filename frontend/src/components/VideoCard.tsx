import { Link } from "react-router-dom";
import type { VideoListItem } from "../api/types";
import { formatDuration, formatRelative, formatViews } from "../lib/format";
import { safeUrl } from "../lib/safeUrl";
import { UserText } from "./UserText";
import styles from "./VideoCard.module.css";

/** Первая буква названия канала для аватара: целый символ (эмодзи — не половиной пары). */
export function initial(text: string): string {
  return (Array.from(text.trim())[0] ?? "?").toUpperCase();
}

export function VideoCard({ video }: { video: VideoListItem }) {
  const watch = `/watch/${video.id}`;
  return (
    <article className={styles.card}>
      <Link to={watch} className={styles.thumb} tabIndex={-1} aria-hidden="true">
        {safeUrl(video.thumbnail_url) ? (
          <img src={safeUrl(video.thumbnail_url)} alt="" loading="lazy" decoding="async" />
        ) : (
          <span className={styles.noThumb} />
        )}
        {video.duration_seconds !== null && (
          <span className={styles.duration}>{formatDuration(video.duration_seconds)}</span>
        )}
      </Link>
      <div className={styles.info}>
        <span className={styles.avatar} aria-hidden="true">
          {initial(video.channel.title)}
        </span>
        <div className={styles.text}>
          <Link to={watch} className={styles.title}>
            <UserText text={video.title} as="h3" lines={2} title />
          </Link>
          <Link to={`/channel/${encodeURIComponent(video.channel.handle)}`} className={styles.channel}>
            <UserText text={video.channel.title} lines={1} />
          </Link>
          <p className={styles.meta}>
            {formatViews(video.views_count)} · <time dateTime={video.published_at}>{formatRelative(video.published_at)}</time>
          </p>
        </div>
      </div>
    </article>
  );
}
