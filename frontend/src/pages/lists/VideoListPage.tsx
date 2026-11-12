import type { ReactNode } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { errorMessage } from "../../api/client";
import type { VideoPage } from "../../api/types";
import { fetchLiked, fetchSubscriptionsFeed, likedKey, subscriptionsFeedKey } from "../../api/videos";
import { InfiniteFooter } from "../../components/InfiniteFooter";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { VideoCard } from "../../components/VideoCard";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import styles from "../HomePage.module.css";

interface Props {
  title: string;
  queryKey: readonly unknown[];
  fetchPage: (cursor: string | null, signal?: AbortSignal) => Promise<VideoPage>;
  emptyTitle: string;
  emptyText: ReactNode;
}

/** Сетка видео с подгрузкой по курсору (лента подписок, «Понравившиеся»). */
function VideoListPage({ title, queryKey, fetchPage, emptyTitle, emptyText }: Props) {
  useDocumentTitle(title);
  const videos = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }) => fetchPage(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = videos.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section aria-labelledby="list-title">
      <h1 id="list-title" className={styles.heading}>
        {title}
      </h1>
      {videos.isPending ? (
        <Spinner />
      ) : videos.isError && !videos.data ? (
        <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={() => void videos.refetch()}>
          {errorMessage(videos.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <StateMessage title={emptyTitle}>{emptyText}</StateMessage>
      ) : (
        <>
          <div className={styles.grid}>
            {items.map((video) => (
              <VideoCard key={video.id} video={video} />
            ))}
          </div>
          <InfiniteFooter query={videos} />
        </>
      )}
    </section>
  );
}

export function SubscriptionsPage() {
  return (
    <VideoListPage
      title="Подписки"
      queryKey={subscriptionsFeedKey}
      fetchPage={fetchSubscriptionsFeed}
      emptyTitle="Здесь появятся видео ваших подписок"
      emptyText="Подпишитесь на канал на странице видео или канала."
    />
  );
}

export function LikedPage() {
  return (
    <VideoListPage
      title="Понравившиеся"
      queryKey={likedKey}
      fetchPage={fetchLiked}
      emptyTitle="Понравившихся видео пока нет"
      emptyText="Нажмите «Нравится» под видео — оно появится здесь."
    />
  );
}
