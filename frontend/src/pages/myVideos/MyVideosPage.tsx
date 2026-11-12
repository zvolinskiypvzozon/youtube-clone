import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, errorMessage, type FieldErrors } from "../../api/client";
import { deleteVideo, fetchMyVideos, myVideosKey, updateVideo } from "../../api/myVideos";
import type { MyVideoItem, VideoUpdateIn, VideoVisibility } from "../../api/types";
import { DESCRIPTION_MAX, TITLE_MAX } from "../../api/uploads";
import { fetchVideo, videoKey } from "../../api/videos";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { formatDuration, formatRelative, formatViews } from "../../lib/format";
import { safeUrl } from "../../lib/safeUrl";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { VISIBILITY_OPTIONS } from "../upload/UploadPage";
import { AccessPanel } from "./AccessPanel";
import styles from "./MyVideosPage.module.css";

/**
 * После удаления или правки видео — списки, где оно могло быть (лента, поиск, каналы,
 * плейлисты), устарели; ссылки потока удалённого видео — больше не нужны.
 */
async function refreshAfterChange(queryClient: ReturnType<typeof useQueryClient>, videoId: string, removed: boolean) {
  queryClient.removeQueries({ queryKey: ["stream", videoId] });
  if (removed) queryClient.removeQueries({ queryKey: videoKey(videoId) });
  await Promise.all(
    [myVideosKey, ["videos"], ["channel"], ["playlist"], ["playlists"]].map((queryKey) =>
      queryClient.invalidateQueries({ queryKey }),
    ),
  );
}

/** Пока что-то обрабатывается, список перезапрашивается. Брошенные загрузки (uploading) — нет. */
export const MY_VIDEOS_POLL_MS = 5000;

const STATUS: Record<string, string> = {
  uploading: "Загружается",
  processing: "Обрабатывается",
  ready: "Готово",
  failed: "Ошибка обработки",
};
const VISIBILITY = Object.fromEntries(VISIBILITY_OPTIONS.map(([value, label]) => [value, label]));

export function MyVideosPage() {
  useDocumentTitle("Мои видео");
  const videos = useInfiniteQuery({
    queryKey: myVideosKey,
    queryFn: ({ pageParam, signal }) => fetchMyVideos(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // При ошибке опрос не продолжается (иначе — запросы по устаревшим данным вдобавок к
    // повторам); при следующем успешном ответе возобновится.
    refetchInterval: (query) =>
      query.state.status !== "error" &&
      query.state.data?.pages.some((p) => p.items.some((v) => v.state.status === "processing"))
        ? MY_VIDEOS_POLL_MS
        : false,
  });
  const items = videos.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section className={styles.page} aria-labelledby="my-videos-title">
      <div className={styles.header}>
        <h1 id="my-videos-title" className={styles.title}>
          Мои видео
        </h1>
        <Link to="/upload" className="button button-primary">
          Загрузить видео
        </Link>
      </div>

      {videos.isPending ? (
        <Spinner />
      ) : videos.isError && !videos.data ? (
        <StateMessage title="Не удалось загрузить список" role="alert" onRetry={() => void videos.refetch()}>
          {errorMessage(videos.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <StateMessage title="У вас пока нет видео">
          <Link to="/upload">Загрузите первое видео</Link> — оно появится здесь.
        </StateMessage>
      ) : (
        <ul className={styles.list}>
          {items.map((video) => (
            <MyVideoRow key={video.id} video={video} />
          ))}
        </ul>
      )}

      {videos.isFetchNextPageError && (
        <StateMessage title="Не удалось загрузить продолжение" role="alert" onRetry={() => void videos.fetchNextPage()}>
          {errorMessage(videos.error)}
        </StateMessage>
      )}
      {videos.isFetchingNextPage ? (
        <Spinner />
      ) : (
        videos.hasNextPage &&
        !videos.isFetchNextPageError && (
          <button type="button" className="button button-secondary" onClick={() => void videos.fetchNextPage()}>
            Показать ещё
          </button>
        )
      )}
    </section>
  );
}

type Panel = "none" | "edit" | "delete" | "access";

function MyVideoRow({ video }: { video: MyVideoItem }) {
  const queryClient = useQueryClient();
  const [panel, setPanel] = useState<Panel>("none");
  const { state } = video;
  const watch = `/watch/${encodeURIComponent(video.id)}`;
  const thumb = safeUrl(video.thumbnail_url);
  const toggle = (next: Panel) => setPanel((p) => (p === next ? "none" : next));

  const remove = useMutation({
    mutationFn: () => deleteVideo(video.id),
    onSuccess: () => refreshAfterChange(queryClient, video.id, true),
  });

  return (
    <li className={styles.row}>
      <Link to={watch} className={styles.thumb} tabIndex={-1} aria-hidden="true">
        {thumb ? <img src={safeUrl(thumb)} alt="" loading="lazy" /> : <span className={styles.noThumb} />}
        {video.duration_seconds !== null && <span className={styles.duration}>{formatDuration(video.duration_seconds)}</span>}
      </Link>
      <div className={styles.info}>
        <Link to={watch} className={styles.videoTitle}>
          <UserText text={video.title} as="h2" lines={2} title />
        </Link>
        <p className={styles.meta}>
          <span className={`${styles.badge} ${styles[`status_${state.status}`] ?? ""}`}>{STATUS[state.status] ?? state.status}</span>
          <span className={styles.badge}>{VISIBILITY[state.visibility] ?? state.visibility}</span>
          {state.moderation === "hidden" && <span className={`${styles.badge} ${styles.status_failed}`}>Скрыто модерацией</span>}
          <span>{formatViews(video.views_count)}</span>
          <time dateTime={video.created_at}>{formatRelative(video.created_at)}</time>
        </p>
        {state.status === "failed" && state.failure_reason && (
          <p className={styles.reason}>
            Причина: <UserText text={state.failure_reason} />
          </p>
        )}
        {state.moderation === "hidden" && state.moderation_reason && (
          <p className={styles.reason}>
            Причина скрытия: <UserText text={state.moderation_reason} className={styles.preWrap} />
          </p>
        )}

        <div className={styles.actions}>
          <button type="button" className={styles.linkButton} aria-expanded={panel === "edit"} onClick={() => toggle("edit")}>
            Изменить
          </button>
          <button type="button" className={styles.linkButton} aria-expanded={panel === "access"} onClick={() => toggle("access")}>
            Доступ
          </button>
          <button type="button" className={styles.linkButton} aria-expanded={panel === "delete"} onClick={() => toggle("delete")}>
            Удалить
          </button>
        </div>

        {panel === "edit" && <EditForm video={video} onClose={() => setPanel("none")} />}
        {panel === "access" && <AccessPanel videoId={video.id} isPrivate={state.visibility === "private"} />}
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
      </div>
    </li>
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
    <div className={styles.panel} role="group" aria-label="Удалить видео?">
      <p className={styles.panelText}>Удалить видео вместе с файлами? Это нельзя отменить.</p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button ref={confirmRef} type="button" className="button button-primary" disabled={pending} onClick={onConfirm}>
          Удалить навсегда
        </button>
        <button type="button" className="button button-secondary" onClick={onCancel}>
          Отмена
        </button>
      </div>
    </div>
  );
}

/** Правка: название, описание (берётся из карточки видео), доступ. Уходят только изменённые поля. */
function EditForm({ video, onClose }: { video: MyVideoItem; onClose: () => void }) {
  const card = useQuery({ queryKey: videoKey(video.id), queryFn: ({ signal }) => fetchVideo(video.id, signal) });
  if (card.isPending) return <Spinner />;
  if (card.isError) {
    return (
      <StateMessage title="Не удалось открыть видео" role="alert" onRetry={() => void card.refetch()}>
        {errorMessage(card.error)}
      </StateMessage>
    );
  }
  return (
    <EditFields
      videoId={video.id}
      initial={{ title: card.data.title, description: card.data.description, visibility: video.state.visibility }}
      onClose={onClose}
    />
  );
}

interface EditValues {
  title: string;
  description: string;
  visibility: VideoVisibility;
}

export function changedFields(initial: EditValues, next: EditValues): VideoUpdateIn {
  const out: VideoUpdateIn = {};
  if (next.title !== initial.title) out.title = next.title;
  if (next.description !== initial.description) out.description = next.description;
  if (next.visibility !== initial.visibility) out.visibility = next.visibility;
  return out;
}

function EditFields({ videoId, initial, onClose }: { videoId: string; initial: EditValues; onClose: () => void }) {
  const queryClient = useQueryClient();
  const ids = { title: useId(), description: useId() };
  const [values, setValues] = useState<EditValues>(initial);
  const [fields, setFields] = useState<FieldErrors>({});
  const titleRef = useRef<HTMLInputElement>(null);
  useEffect(() => titleRef.current?.focus(), []);
  const changes = changedFields(initial, values);
  const mutation = useMutation({
    mutationFn: () => updateVideo(videoId, changes),
    onSuccess: async (card) => {
      queryClient.setQueryData(videoKey(videoId), card);
      await refreshAfterChange(queryClient, videoId, false);
      onClose();
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 422) setFields(error.fields);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setFields({});
    if (!values.title.trim()) {
      setFields({ title: "Введите название" });
      return;
    }
    if (Object.keys(changes).length) mutation.mutate();
    else onClose();
  }

  const generalError =
    mutation.isError && !(mutation.error instanceof ApiError && mutation.error.status === 422 && (fields.title || fields.description))
      ? errorMessage(mutation.error)
      : null;

  return (
    <form className={styles.panel} onSubmit={submit} noValidate aria-label="Изменить видео">
      <label htmlFor={ids.title} className={styles.label}>
        Название
      </label>
      <input
        id={ids.title}
        ref={titleRef}
        dir="auto"
        className={styles.input}
        maxLength={TITLE_MAX}
        value={values.title}
        aria-invalid={fields.title ? true : undefined}
        aria-describedby={fields.title ? `${ids.title}-error` : undefined}
        onChange={(e) => setValues({ ...values, title: e.target.value })}
      />
      {fields.title && (
        <span id={`${ids.title}-error`} className={styles.error}>
          {fields.title}
        </span>
      )}
      <label htmlFor={ids.description} className={styles.label}>
        Описание
      </label>
      <textarea
        id={ids.description}
        dir="auto"
        className={styles.input}
        rows={4}
        maxLength={DESCRIPTION_MAX}
        value={values.description}
        aria-invalid={fields.description ? true : undefined}
        aria-describedby={fields.description ? `${ids.description}-error` : undefined}
        onChange={(e) => setValues({ ...values, description: e.target.value })}
      />
      {fields.description && (
        <span id={`${ids.description}-error`} className={styles.error}>
          {fields.description}
        </span>
      )}
      <fieldset className={styles.radios}>
        <legend className={styles.label}>Доступ</legend>
        {VISIBILITY_OPTIONS.map(([value, label]) => (
          <label key={value} className={styles.radio}>
            <input
              type="radio"
              name={`visibility-${videoId}`}
              value={value}
              checked={values.visibility === value}
              onChange={() => setValues({ ...values, visibility: value })}
            />
            {label}
          </label>
        ))}
      </fieldset>
      {generalError && (
        <p className={styles.error} role="alert">
          {generalError}
        </p>
      )}
      <div className={styles.actions}>
        <button type="submit" className="button button-primary" disabled={mutation.isPending}>
          Сохранить
        </button>
        <button type="button" className="button button-secondary" onClick={onClose}>
          Отмена
        </button>
      </div>
    </form>
  );
}
