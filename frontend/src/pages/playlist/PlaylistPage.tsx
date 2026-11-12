import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { errorMessage } from "../../api/client";
import { isNotFound } from "../channel/ChannelPage";
import {
  deletePlaylist,
  fetchPlaylist,
  fetchPlaylistVideos,
  PLAYLIST_LIST_KEYS,
  playlistKey,
  playlistVideosKey,
  removeFromPlaylist,
  updatePlaylist,
} from "../../api/playlists";
import type { Playlist, PlaylistUpdateIn, PlaylistVideoItem } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { InfiniteFooter } from "../../components/InfiniteFooter";
import { formatVideoCount, PLAYLIST_VISIBILITY } from "../../components/PlaylistCard";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { formatDuration, formatViews } from "../../lib/format";
import { safeUrl } from "../../lib/safeUrl";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { PlaylistForm, type PlaylistValues } from "./PlaylistForm";
import styles from "./PlaylistPage.module.css";

export function PlaylistPage() {
  // UUID — в нижнем регистре, как в ответах API (ключи кэша совпадают).
  const playlistId = (useParams().playlistId ?? "").toLowerCase();
  const playlist = useQuery({
    queryKey: playlistKey(playlistId),
    queryFn: ({ signal }) => fetchPlaylist(playlistId, signal),
  });
  useDocumentTitle(playlist.data?.title ?? null);

  if (playlist.isPending) return <Spinner />;
  if (playlist.isError) {
    // 404 — нет, приватный чужой или канал заблокирован: не различаем.
    if (isNotFound(playlist.error)) {
      return (
        <StateMessage title="Плейлист не найден" role="alert">
          Плейлист удалён или доступ к нему закрыт. <Link to="/">На главную</Link>
        </StateMessage>
      );
    }
    return (
      <StateMessage title="Не удалось загрузить плейлист" role="alert" onRetry={() => void playlist.refetch()}>
        {errorMessage(playlist.error)}
      </StateMessage>
    );
  }
  return <PlaylistContent key={playlist.data.id} playlist={playlist.data} />;
}

export function changedPlaylistFields(initial: PlaylistValues, next: PlaylistValues): PlaylistUpdateIn {
  const out: PlaylistUpdateIn = {};
  if (next.title !== initial.title) out.title = next.title;
  if (next.description !== initial.description) out.description = next.description;
  if (next.visibility !== initial.visibility) out.visibility = next.visibility;
  return out;
}

function PlaylistContent({ playlist }: { playlist: Playlist }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [panel, setPanel] = useState<"none" | "edit" | "delete">("none");
  const channelPath = `/channel/${encodeURIComponent(playlist.channel.handle)}`;
  const refreshLists = () =>
    Promise.all(PLAYLIST_LIST_KEYS.map((queryKey) => queryClient.invalidateQueries({ queryKey })));

  const remove = useMutation({
    mutationFn: () => deletePlaylist(playlist.id),
    onSuccess: async () => {
      // Сначала уйти со страницы, затем убрать её данные: иначе смонтированный запрос
      // перезапросил бы удалённый плейлист и мелькнуло бы «не найден» (аудит дня 8).
      const handle = user?.channel.handle ?? playlist.channel.handle;
      navigate(`/channel/${encodeURIComponent(handle)}?tab=playlists`, { replace: true });
      await queryClient.cancelQueries({ queryKey: playlistKey(playlist.id) });
      queryClient.removeQueries({ queryKey: playlistKey(playlist.id) });
      queryClient.removeQueries({ queryKey: playlistVideosKey(playlist.id) });
      await refreshLists();
    },
  });

  const initial: PlaylistValues = {
    title: playlist.title,
    description: playlist.description,
    visibility: playlist.visibility ?? "public",
  };

  return (
    <section className={styles.page} aria-labelledby="playlist-title">
      <header className={styles.header}>
        <UserText text={playlist.title} as="h1" id="playlist-title" lines={2} className={styles.title} title />
        <p className={styles.meta}>
          <Link to={channelPath} className={styles.channel}>
            <UserText text={playlist.channel.title} lines={1} />
          </Link>
          <span>{formatVideoCount(playlist.video_count)}</span>
          {playlist.can_manage && playlist.visibility && (
            <span className={styles.badge}>{PLAYLIST_VISIBILITY[playlist.visibility] ?? playlist.visibility}</span>
          )}
        </p>
        {playlist.description && <UserText text={playlist.description} as="p" lines={4} className={styles.description} />}
        {playlist.can_manage && (
          <div className={styles.actions}>
            <button type="button" className="button button-secondary" aria-expanded={panel === "edit"} onClick={() => setPanel(panel === "edit" ? "none" : "edit")}>
              Изменить
            </button>
            <button type="button" className="button button-secondary" aria-expanded={panel === "delete"} onClick={() => setPanel(panel === "delete" ? "none" : "delete")}>
              Удалить плейлист
            </button>
          </div>
        )}
        {panel === "edit" && (
          <PlaylistForm
            initial={initial}
            submitLabel="Сохранить"
            onCancel={() => setPanel("none")}
            onSubmit={async (values) => {
              const changes = changedPlaylistFields(initial, values);
              if (Object.keys(changes).length) {
                const updated = await updatePlaylist(playlist.id, changes);
                queryClient.setQueryData(playlistKey(playlist.id), updated);
                await refreshLists();
              }
              setPanel("none");
            }}
          />
        )}
        {panel === "delete" && (
          <DeleteConfirm
            pending={remove.isPending}
            error={remove.isError ? errorMessage(remove.error) : null}
            onConfirm={() => remove.mutate()}
            onCancel={() => {
              remove.reset();
              setPanel("none");
            }}
          />
        )}
      </header>

      <PlaylistVideos playlist={playlist} />
    </section>
  );
}

function DeleteConfirm({
  pending,
  error,
  onConfirm,
  onCancel,
}: {
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => confirmRef.current?.focus(), []);
  return (
    <div className={styles.confirm} role="group" aria-label="Удалить плейлист?">
      <p className={styles.muted}>Удалить плейлист? Видео в нём не удаляются.</p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button ref={confirmRef} type="button" className="button button-primary" disabled={pending} onClick={onConfirm}>
          Удалить
        </button>
        <button type="button" className="button button-secondary" onClick={onCancel}>
          Отмена
        </button>
      </div>
    </div>
  );
}

const STATUS: Record<string, string> = {
  uploading: "Загружается",
  processing: "Обрабатывается",
  failed: "Ошибка обработки",
};

function PlaylistVideos({ playlist }: { playlist: Playlist }) {
  const queryClient = useQueryClient();
  const videos = useInfiniteQuery({
    queryKey: playlistVideosKey(playlist.id),
    queryFn: ({ pageParam, signal }) => fetchPlaylistVideos(playlist.id, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = videos.data?.pages.flatMap((p) => p.items) ?? [];
  const [error, setError] = useState<string | null>(null);
  const removeItem = useMutation({
    mutationFn: (videoId: string) => removeFromPlaylist(playlist.id, videoId),
    onMutate: () => setError(null),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: playlistVideosKey(playlist.id) }),
        queryClient.invalidateQueries({ queryKey: playlistKey(playlist.id) }),
        queryClient.invalidateQueries({ queryKey: ["playlists"] }),
        queryClient.invalidateQueries({ queryKey: ["channel-playlists"] }),
      ]),
    onError: (e) => setError(errorMessage(e)),
  });

  if (videos.isPending) return <Spinner />;
  if (videos.isError && !videos.data) {
    return (
      <StateMessage title="Не удалось загрузить видео плейлиста" role="alert" onRetry={() => void videos.refetch()}>
        {errorMessage(videos.error)}
      </StateMessage>
    );
  }
  if (items.length === 0) {
    return (
      <StateMessage title="В плейлисте пока нет видео">
        {playlist.can_manage ? "Добавить своё видео можно кнопкой «Сохранить» на странице видео." : null}
      </StateMessage>
    );
  }
  return (
    <>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <ol className={styles.list}>
        {items.map((video, index) => (
          <PlaylistVideoRow
            key={video.id}
            video={video}
            index={index + 1}
            canManage={playlist.can_manage}
            removing={removeItem.isPending && removeItem.variables === video.id}
            onRemove={() => removeItem.mutate(video.id)}
          />
        ))}
      </ol>
      <InfiniteFooter query={videos} />
    </>
  );
}

function PlaylistVideoRow({
  video,
  index,
  canManage,
  removing,
  onRemove,
}: {
  video: PlaylistVideoItem;
  index: number;
  canManage: boolean;
  removing: boolean;
  onRemove: () => void;
}) {
  const watch = `/watch/${encodeURIComponent(video.id)}`;
  const thumb = safeUrl(video.thumbnail_url);
  const status = video.state && video.state.status !== "ready" ? STATUS[video.state.status] : null;
  return (
    <li className={styles.row}>
      <span className={styles.index} aria-hidden="true">
        {index}
      </span>
      <Link to={watch} className={styles.thumb} tabIndex={-1} aria-hidden="true">
        {thumb ? <img src={safeUrl(thumb)} alt="" loading="lazy" /> : <span className={styles.noThumb} />}
        {video.duration_seconds !== null && <span className={styles.duration}>{formatDuration(video.duration_seconds)}</span>}
      </Link>
      <div className={styles.info}>
        <Link to={watch} className={styles.videoTitle}>
          <UserText text={video.title} as="h2" lines={2} title />
        </Link>
        <p className={styles.muted}>
          <UserText text={video.channel.title} lines={1} /> · {formatViews(video.views_count)}
          {status && <span className={styles.badge}>{status}</span>}
          {video.state?.moderation === "hidden" && <span className={styles.badge}>Скрыто модерацией</span>}
        </p>
      </div>
      {canManage && (
        <button type="button" className={styles.linkButton} disabled={removing} onClick={onRemove} aria-label={`Убрать из плейлиста: ${video.title}`}>
          Убрать
        </button>
      )}
    </li>
  );
}
