import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { ApiError, errorMessage } from "../../api/client";
import {
  addToPlaylist,
  createPlaylist,
  fetchMyPlaylists,
  myPlaylistsKey,
  PLAYLIST_LIST_KEYS,
  removeFromPlaylist,
} from "../../api/playlists";
import type { PlaylistPage } from "../../api/types";
import { InfiniteFooter } from "../../components/InfiniteFooter";
import { PLAYLIST_VISIBILITY } from "../../components/PlaylistCard";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { PlaylistForm } from "../playlist/PlaylistForm";
import styles from "./SaveToPlaylist.module.css";

type Pages = InfiniteData<PlaylistPage, string | null>;

/**
 * «Сохранить в плейлист» — только на странице своего видео: в плейлист добавляются лишь свои
 * видео (решение дня 6; сервер ответит 403 на чужое). Отметка — contains_video с сервера.
 */
export function SaveToPlaylist({ videoId }: { videoId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={styles.wrap}>
      <button type="button" className="button button-secondary" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        Сохранить в плейлист
      </button>
      {open && <SavePanel videoId={videoId} />}
    </div>
  );
}

function SavePanel({ videoId }: { videoId: string }) {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = myPlaylistsKey(videoId);
  const playlists = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam, signal }) => fetchMyPlaylists(pageParam, videoId, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });

  const mark = (playlistId: string, contains: boolean) =>
    queryClient.setQueryData<Pages>(key, (old) =>
      old
        ? {
            ...old,
            pages: old.pages.map((p) => ({
              ...p,
              items: p.items.map((it) =>
                it.id === playlistId ? { ...it, contains_video: contains } : it,
              ),
            })),
          }
        : old,
    );

  // Прочие списки и составы плейлистов устарели (счётчики, превью).
  const refreshOthers = () =>
    Promise.all(
      PLAYLIST_LIST_KEYS.map((queryKey) =>
        queryClient.invalidateQueries({ queryKey, predicate: (q) => JSON.stringify(q.queryKey) !== JSON.stringify(key) }),
      ),
    );

  const toggle = useMutation({
    mutationFn: async ({ playlistId, add }: { playlistId: string; add: boolean }): Promise<void> => {
      if (add) await addToPlaylist(playlistId, videoId);
      else await removeFromPlaylist(playlistId, videoId);
    },
    onMutate: async () => {
      setError(null);
      await queryClient.cancelQueries({ queryKey: key, exact: true });
    },
    onSuccess: async (_out, { playlistId, add }) => {
      mark(playlistId, add);
      await refreshOthers();
    },
    onError: (e) => {
      setError(errorMessage(e));
      // Плейлист удалён или недоступен — обновить список, чтобы он пропал.
      if (e instanceof ApiError && e.status === 404) void queryClient.invalidateQueries({ queryKey: key, exact: true });
    },
  });

  const items = playlists.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className={styles.panel} role="group" aria-label="Сохранить в плейлист">
      {playlists.isPending ? (
        <Spinner />
      ) : playlists.isError && !playlists.data ? (
        <StateMessage title="Не удалось загрузить плейлисты" role="alert" onRetry={() => void playlists.refetch()}>
          {errorMessage(playlists.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <p className={styles.muted}>Плейлистов пока нет — создайте первый.</p>
      ) : (
        <ul className={styles.list}>
          {items.map((p) => (
            <li key={p.id}>
              <label className={styles.item}>
                <input
                  type="checkbox"
                  checked={Boolean(p.contains_video)}
                  // aria-disabled, а не disabled: disabled сбросил бы фокус с нажатой галочки.
                  aria-disabled={toggle.isPending || undefined}
                  onChange={(e) => {
                    if (!toggle.isPending) toggle.mutate({ playlistId: p.id, add: e.target.checked });
                  }}
                />
                <UserText text={p.title} lines={1} className={styles.title} />
                {p.visibility && <span className={styles.badge}>{PLAYLIST_VISIBILITY[p.visibility] ?? p.visibility}</span>}
              </label>
            </li>
          ))}
        </ul>
      )}
      <InfiniteFooter query={playlists} />
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      {creating ? (
        <PlaylistForm
          compact
          submitLabel="Создать и добавить"
          onCancel={() => setCreating(false)}
          onSubmit={async (values) => {
            // Ошибка создания — остаётся в форме (повтор безопасен). После создания форма
            // закрывается в любом случае: повторная отправка создала бы второй плейлист
            // (ревью дня 8); не добавилось — сообщение, отметить можно галочкой.
            const created = await createPlaylist(values);
            setCreating(false);
            try {
              await addToPlaylist(created.id, videoId);
            } catch (e) {
              setError(`Плейлист «${created.title}» создан, но видео не добавлено: ${errorMessage(e)}`);
            }
            await Promise.all([queryClient.invalidateQueries({ queryKey: key, exact: true }), refreshOthers()]);
          }}
        />
      ) : (
        <button type="button" className={styles.linkButton} onClick={() => setCreating(true)}>
          + Новый плейлист
        </button>
      )}
    </div>
  );
}
