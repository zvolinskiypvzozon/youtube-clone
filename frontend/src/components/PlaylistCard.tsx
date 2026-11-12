import { Link } from "react-router-dom";
import type { PlaylistListItem } from "../api/types";
import { safeUrl } from "../lib/safeUrl";
import { UserText } from "./UserText";
import styles from "./PlaylistCard.module.css";

export const PLAYLIST_VISIBILITY: Record<string, string> = {
  public: "Открытый",
  unlisted: "По ссылке",
  private: "Только я",
};

/** «видео» не склоняется: «1 видео», «5 видео». */
export const formatVideoCount = (n: number) => `${n.toLocaleString("ru-RU")} видео`;

export function PlaylistCard({ playlist }: { playlist: PlaylistListItem }) {
  const to = `/playlist/${encodeURIComponent(playlist.id)}`;
  const thumb = safeUrl(playlist.thumbnail_url);
  return (
    <article className={styles.card}>
      <Link to={to} className={styles.thumb} tabIndex={-1} aria-hidden="true">
        {thumb ? <img src={safeUrl(thumb)} alt="" loading="lazy" /> : <span className={styles.noThumb} />}
        <span className={styles.count}>{formatVideoCount(playlist.video_count)}</span>
      </Link>
      <Link to={to} className={styles.title}>
        <UserText text={playlist.title} as="h3" lines={2} title />
      </Link>
      {playlist.visibility && <span className={styles.badge}>{PLAYLIST_VISIBILITY[playlist.visibility] ?? playlist.visibility}</span>}
    </article>
  );
}
