import { useRef, useState, type KeyboardEvent } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError, errorMessage } from "../../api/client";
import { channelKey, fetchChannel } from "../../api/channels";
import {
  channelPlaylistsKey,
  channelVideosKey,
  createPlaylist,
  fetchChannelPlaylists,
  fetchChannelVideos,
  fetchMyPlaylists,
  myPlaylistsKey,
  PLAYLIST_LIST_KEYS,
} from "../../api/playlists";
import type { Channel } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { InfiniteFooter } from "../../components/InfiniteFooter";
import { PlaylistCard } from "../../components/PlaylistCard";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { SubscribeButton } from "../../components/SubscribeButton";
import { UserText } from "../../components/UserText";
import { initial, VideoCard } from "../../components/VideoCard";
import { formatSubscribers } from "../../lib/format";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { PlaylistForm } from "../playlist/PlaylistForm";
import styles from "./ChannelPage.module.css";

type Tab = "videos" | "playlists";

const TABS: [Tab, string][] = [
  ["videos", "Видео"],
  ["playlists", "Плейлисты"],
];

/** 422 у карточки — неверный формат ника или id в адресе: для пользователя это «не найдено». */
export const isNotFound = (error: unknown) => error instanceof ApiError && (error.status === 404 || error.status === 422);

export function ChannelPage() {
  const { handle = "" } = useParams();
  const channel = useQuery({ queryKey: channelKey(handle), queryFn: ({ signal }) => fetchChannel(handle, signal) });
  useDocumentTitle(channel.data?.title ?? null);

  if (channel.isPending) return <Spinner />;
  if (channel.isError) {
    // 404 — нет канала или автор заблокирован: не различаем.
    if (isNotFound(channel.error)) {
      return (
        <StateMessage title="Канал не найден" role="alert">
          Проверьте адрес или вернитесь <Link to="/">на главную</Link>.
        </StateMessage>
      );
    }
    return (
      <StateMessage title="Не удалось загрузить канал" role="alert" onRetry={() => void channel.refetch()}>
        {errorMessage(channel.error)}
      </StateMessage>
    );
  }
  return <ChannelContent key={channel.data.id} channel={channel.data} />;
}

function ChannelContent({ channel }: { channel: Channel }) {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "playlists" ? "playlists" : "videos";
  const own = Boolean(user && user.channel.id === channel.id);
  const select = (next: Tab) => setParams(next === "videos" ? {} : { tab: next }, { replace: true });
  const tabRefs = useRef(new Map<Tab, HTMLButtonElement | null>());

  function onTabKey(event: KeyboardEvent<HTMLDivElement>) {
    const current = TABS.findIndex(([value]) => value === tab);
    const last = TABS.length - 1;
    const next =
      event.key === "ArrowRight" ? (current === last ? 0 : current + 1)
      : event.key === "ArrowLeft" ? (current === 0 ? last : current - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (next === null) return;
    event.preventDefault();
    select(TABS[next][0]);
    tabRefs.current.get(TABS[next][0])?.focus();
  }

  return (
    <section className={styles.page} aria-labelledby="channel-title">
      <header className={styles.header}>
        <span className={styles.avatar} aria-hidden="true">
          {initial(channel.title)}
        </span>
        <div className={styles.headerText}>
          <UserText text={channel.title} as="h1" id="channel-title" lines={1} className={styles.title} title />
          <p className={styles.meta}>
            <UserText text={`@${channel.handle}`} /> · {formatSubscribers(channel.subscribers_count)}
          </p>
          {channel.description && (
            <UserText text={channel.description} as="p" lines={2} className={styles.description} />
          )}
          <div className={styles.headerActions}>
            {own ? (
              <>
                <Link to="/my/videos" className="button button-secondary">
                  Мои видео
                </Link>
                <Link to="/upload" className="button button-secondary">
                  Загрузить видео
                </Link>
              </>
            ) : (
              <SubscribeButton channel={channel} />
            )}
          </div>
        </div>
      </header>

      {/* Вкладки по шаблону WAI-ARIA: в порядке Tab — только активная (roving tabindex),
          стрелки и Home/End переключают и переводят фокус (ревью дня 8). */}
      <div className={styles.tabs} role="tablist" aria-label="Разделы канала" onKeyDown={onTabKey}>
        {TABS.map(([value, label]) => (
          <button
            key={value}
            ref={(el) => {
              tabRefs.current.set(value, el);
            }}
            type="button"
            role="tab"
            id={`tab-${value}`}
            aria-selected={tab === value}
            aria-controls={tab === value ? `panel-${value}` : undefined}
            tabIndex={tab === value ? 0 : -1}
            className={styles.tab}
            onClick={() => select(value)}
          >
            {label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} tabIndex={0} className={styles.panel}>
        {tab === "videos" ? (
          <ChannelVideos handle={channel.handle} own={own} />
        ) : user === undefined ? (
          // Пока неизвестно, свой ли канал, — не запрашиваем открытые плейлисты (мелькнули бы).
          <Spinner />
        ) : own ? (
          <OwnPlaylists />
        ) : (
          <ChannelPlaylists handle={channel.handle} />
        )}
      </div>
    </section>
  );
}

function ChannelVideos({ handle, own }: { handle: string; own: boolean }) {
  const videos = useInfiniteQuery({
    queryKey: channelVideosKey(handle),
    queryFn: ({ pageParam, signal }) => fetchChannelVideos(handle, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = videos.data?.pages.flatMap((p) => p.items) ?? [];
  if (videos.isPending) return <Spinner />;
  if (videos.isError && !videos.data) {
    return (
      <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={() => void videos.refetch()}>
        {errorMessage(videos.error)}
      </StateMessage>
    );
  }
  if (items.length === 0) {
    return (
      <StateMessage title="На канале пока нет видео">
        {own ? (
          <>
            Здесь показываются готовые видео с открытым доступом. <Link to="/my/videos">Все ваши видео</Link>
          </>
        ) : null}
      </StateMessage>
    );
  }
  return (
    <>
      <div className={styles.grid}>
        {items.map((video) => (
          <VideoCard key={video.id} video={video} />
        ))}
      </div>
      <InfiniteFooter query={videos} />
    </>
  );
}

function ChannelPlaylists({ handle }: { handle: string }) {
  const playlists = useInfiniteQuery({
    queryKey: channelPlaylistsKey(handle),
    queryFn: ({ pageParam, signal }) => fetchChannelPlaylists(handle, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = playlists.data?.pages.flatMap((p) => p.items) ?? [];
  if (playlists.isPending) return <Spinner />;
  if (playlists.isError && !playlists.data) {
    return (
      <StateMessage title="Не удалось загрузить плейлисты" role="alert" onRetry={() => void playlists.refetch()}>
        {errorMessage(playlists.error)}
      </StateMessage>
    );
  }
  if (items.length === 0) return <StateMessage title="Открытых плейлистов нет" />;
  return (
    <>
      <div className={styles.grid}>
        {items.map((p) => (
          <PlaylistCard key={p.id} playlist={p} />
        ))}
      </div>
      <InfiniteFooter query={playlists} />
    </>
  );
}

/** Свой канал: все свои плейлисты (с видимостью) и создание. */
function OwnPlaylists() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const playlists = useInfiniteQuery({
    queryKey: myPlaylistsKey(),
    queryFn: ({ pageParam, signal }) => fetchMyPlaylists(pageParam, undefined, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = playlists.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
      {creating ? (
        <PlaylistForm
          submitLabel="Создать плейлист"
          onCancel={() => setCreating(false)}
          onSubmit={async (values) => {
            const created = await createPlaylist(values);
            await Promise.all(PLAYLIST_LIST_KEYS.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
            navigate(`/playlist/${encodeURIComponent(created.id)}`);
          }}
        />
      ) : (
        <div>
          <button type="button" className="button button-primary" onClick={() => setCreating(true)}>
            Создать плейлист
          </button>
        </div>
      )}
      {playlists.isPending ? (
        <Spinner />
      ) : playlists.isError && !playlists.data ? (
        <StateMessage title="Не удалось загрузить плейлисты" role="alert" onRetry={() => void playlists.refetch()}>
          {errorMessage(playlists.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <StateMessage title="У вас пока нет плейлистов" />
      ) : (
        <>
          <div className={styles.grid}>
            {items.map((p) => (
              <PlaylistCard key={p.id} playlist={p} />
            ))}
          </div>
          <InfiniteFooter query={playlists} />
        </>
      )}
    </>
  );
}
