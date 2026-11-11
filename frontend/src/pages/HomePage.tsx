import { useEffect, useRef } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { errorMessage } from "../api/client";
import { fetchFeed, SEARCH_MAX } from "../api/videos";
import { Spinner } from "../components/Spinner";
import { StateMessage } from "../components/StateMessage";
import { UserText } from "../components/UserText";
import { VideoCard } from "../components/VideoCard";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import styles from "./HomePage.module.css";

/** Лента главной (новые сверху) и результаты поиска (?q=). Подгрузка — по курсору. */
export function HomePage() {
  const [params] = useSearchParams();
  // Длиннее предела API (422) не отправляем — обрезаем, как поле поиска.
  const q = (params.get("q") ?? "").trim().slice(0, SEARCH_MAX);
  useDocumentTitle(q ? `Поиск: ${q}` : null);

  const feed = useInfiniteQuery({
    queryKey: ["videos", "feed", q],
    queryFn: ({ pageParam, signal }) => fetchFeed(q, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });

  const sentinel = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage } = feed;
  useEffect(() => {
    const node = sentinel.current;
    // После ошибки подгрузки не наблюдаем: новый observer сразу сообщил бы о видимости и
    // запрос пошёл бы снова — бесконечный цикл (ревью дня 7, High). Повтор — по кнопке.
    if (!node || !hasNextPage || isFetchNextPageError || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetchingNextPage) void fetchNextPage();
      },
      { rootMargin: "600px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);

  const videos = feed.data?.pages.flatMap((p) => p.items) ?? [];
  // Ошибка подгрузки продолжения: уже загруженные страницы остаются на экране.
  const nextPageError = isFetchNextPageError ? feed.error : null;

  return (
    <section aria-labelledby="feed-title">
      <h1 id="feed-title" className={q ? styles.heading : "visually-hidden"}>
        {q ? (
          <>
            Результаты по запросу «<UserText text={q} />»
          </>
        ) : (
          "Рекомендации"
        )}
      </h1>

      {feed.isPending ? (
        <Spinner />
      ) : feed.isError && !feed.data ? (
        <StateMessage title="Не удалось загрузить видео" role="alert" onRetry={() => void feed.refetch()}>
          {errorMessage(feed.error)}
        </StateMessage>
      ) : videos.length === 0 ? (
        <StateMessage title={q ? "Ничего не найдено" : "Пока нет видео"}>
          {q ? "Попробуйте другой запрос: поиск идёт по названию видео." : "Здесь появятся опубликованные видео."}
        </StateMessage>
      ) : (
        <>
          <div className={styles.grid}>
            {videos.map((video) => (
              <VideoCard key={video.id} video={video} />
            ))}
          </div>
          <div ref={sentinel} />
          {nextPageError && (
            <StateMessage title="Не удалось загрузить продолжение" role="alert" onRetry={() => void fetchNextPage()}>
              {errorMessage(nextPageError)}
            </StateMessage>
          )}
          {isFetchingNextPage ? (
            <Spinner />
          ) : (
            hasNextPage &&
            !nextPageError && (
              <div className={styles.more}>
                <button type="button" className="button button-secondary" onClick={() => void fetchNextPage()}>
                  Показать ещё
                </button>
              </div>
            )
          )}
        </>
      )}
    </section>
  );
}
