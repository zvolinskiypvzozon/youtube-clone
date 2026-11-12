import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useInfiniteQuery, useMutation, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, errorMessage } from "../../api/client";
import { addComment, COMMENT_MAX, commentsKey, deleteComment, fetchComments } from "../../api/comments";
import type { Comment, CommentPage, VideoCard } from "../../api/types";
import { videoKey } from "../../api/videos";
import { useAuth } from "../../auth/AuthContext";
import { useLoginRedirect } from "../../auth/useLoginRedirect";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { initial } from "../../components/VideoCard";
import { formatComments, formatRelative } from "../../lib/format";
import { refreshAfterModeration, unbanUser } from "../../api/moderation";
import { ReasonAction } from "../../components/ReasonAction";
import { BanAuthorForm } from "./BanAuthorForm";
import styles from "./Comments.module.css";

type Pages = InfiniteData<CommentPage, string | null>;


/** Свёрнут до 8 строк с «Читать полностью»: явно длиннее 8 строк (≈ 90 символов в строке). */
export const isLongComment = (text: string) => text.length > 720 || text.split("\n").length > 8;

/**
 * Комментарии под видео. Текст — только через UserText (обычный текст, dir="auto", переносы
 * строк сохраняются): без HTML, Markdown и автоссылок (security.md, день 5).
 *
 * Счётчик в заголовке — из карточки видео: после добавления, удаления и бана карточка
 * перезапрашивается (сервер не считает комментарии забаненных — локальный ±1 разошёлся бы).
 */
export function Comments({ video }: { video: VideoCard }) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const key = commentsKey(video.id);
  const comments = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam, signal }) => fetchComments(video.id, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [announce, setAnnounce] = useState("");

  // Правка кэша во время подгрузки страницы потерялась бы: fetchNextPage собирает результат из
  // страниц, снятых до запроса (ревью дня 8). Сначала отменяем запрос, затем правим.
  const editPages = useCallback(
    async (edit: (pages: CommentPage[]) => CommentPage[]) => {
      await queryClient.cancelQueries({ queryKey: commentsKey(video.id) });
      queryClient.setQueryData<Pages>(commentsKey(video.id), (old) => (old ? { ...old, pages: edit(old.pages) } : old));
      void queryClient.invalidateQueries({ queryKey: videoKey(video.id) });
    },
    [queryClient, video.id],
  );

  const onAdded = (comment: Comment) =>
    editPages((pages) =>
      pages.some((p) => p.items.some((c) => c.id === comment.id))
        ? pages
        : pages.map((p, i) => (i === 0 ? { ...p, items: [comment, ...p.items] } : p)),
    );

  const onDeleted = async (id: string) => {
    await editPages((pages) => pages.map((p) => ({ ...p, items: p.items.filter((c) => c.id !== id) })));
    setAnnounce("Комментарий удалён");
    headingRef.current?.focus();
  };

  // Бан или разбан из комментария: кнопка исчезает — фокус на заголовок, итог объявляется.
  const onModerated = (message: string) => {
    setAnnounce(message);
    headingRef.current?.focus();
  };

  const items = comments.data?.pages.flatMap((p) => p.items) ?? [];
  // Писать нельзя под скрытым модерацией видео (сервер ответит 409): форму не показываем.
  const hidden = video.state?.moderation === "hidden";

  return (
    <section className={styles.section} aria-labelledby="comments-title">
      <h2 id="comments-title" ref={headingRef} tabIndex={-1} className={styles.heading}>
        {formatComments(video.comments_count)}
      </h2>
      <p className="visually-hidden" role="status">
        {announce}
      </p>

      {hidden ? (
        <p className={styles.muted}>Новые комментарии закрыты: видео скрыто модерацией.</p>
      ) : (
        // key: черновик и ошибка прежнего пользователя не достаются следующему (аудит дня 8).
        <CommentForm key={user?.id ?? "anon"} videoId={video.id} onAdded={onAdded} />
      )}

      {comments.isPending ? (
        <Spinner />
      ) : comments.isError && !comments.data ? (
        <StateMessage title="Не удалось загрузить комментарии" role="alert" onRetry={() => void comments.refetch()}>
          {errorMessage(comments.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <p className={styles.muted}>Комментариев пока нет.</p>
      ) : (
        <ul className={styles.list}>
          {items.map((comment) => (
            <CommentItem
              key={comment.id}
              comment={comment}
              onDeleted={onDeleted}
              onModerated={onModerated}
            />
          ))}
        </ul>
      )}

      {comments.isFetchNextPageError && (
        <StateMessage title="Не удалось загрузить продолжение" role="alert" onRetry={() => void comments.fetchNextPage()}>
          {errorMessage(comments.error)}
        </StateMessage>
      )}
      {comments.isFetchingNextPage ? (
        <Spinner />
      ) : (
        comments.hasNextPage &&
        !comments.isFetchNextPageError && (
          <button type="button" className="button button-secondary" onClick={() => void comments.fetchNextPage()}>
            Показать ещё
          </button>
        )
      )}
    </section>
  );
}

function CommentForm({ videoId, onAdded }: { videoId: string; onAdded: (c: Comment) => Promise<void> }) {
  const { user } = useAuth();
  const toLogin = useLoginRedirect();
  const [body, setBody] = useState("");
  const fieldId = useId();
  const counterId = useId();
  const mutation = useMutation({
    mutationFn: (text: string) => addComment(videoId, text),
    onSuccess: async (comment) => {
      setBody("");
      await onAdded(comment);
    },
  });

  if (user === undefined) return null;
  if (!user) {
    return (
      <p className={styles.muted}>
        <button type="button" className={styles.linkButton} onClick={toLogin}>
          Войдите
        </button>
        , чтобы оставить комментарий.
      </p>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (body.trim()) mutation.mutate(body);
  }

  // 422 — сообщение сервера по полю body (например, «текст без видимых символов»).
  const error =
    mutation.error instanceof ApiError && mutation.error.status === 422
      ? (mutation.error.fields.body ?? mutation.error.message)
      : mutation.isError
        ? errorMessage(mutation.error)
        : null;

  return (
    <form className={styles.form} onSubmit={submit} aria-label="Новый комментарий">
      <label htmlFor={fieldId} className="visually-hidden">
        Комментарий
      </label>
      <textarea
        id={fieldId}
        dir="auto"
        className={styles.textarea}
        placeholder="Введите комментарий"
        rows={2}
        maxLength={COMMENT_MAX}
        value={body}
        aria-describedby={counterId}
        aria-invalid={error ? true : undefined}
        onChange={(e) => {
          setBody(e.target.value);
          if (mutation.isError) mutation.reset();
        }}
      />
      <div className={styles.formBar}>
        <span id={counterId} className={styles.counter}>
          {body.length} / {COMMENT_MAX}
        </span>
        {error && (
          <span className={styles.error} role="alert">
            {error}
          </span>
        )}
        <button type="submit" className="button button-primary" disabled={!body.trim() || mutation.isPending}>
          Комментировать
        </button>
      </div>
    </form>
  );
}

function CommentItem({
  comment,
  onDeleted,
  onModerated,
}: {
  comment: Comment;
  onDeleted: (id: string) => Promise<void>;
  onModerated: (message: string) => void;
}) {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const bodyId = useId();
  const long = isLongComment(comment.body);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);
  const remove = useMutation({
    mutationFn: () => deleteComment(comment.id),
    onSuccess: () => onDeleted(comment.id),
    onError: (error) => {
      // Уже удалён (404) — убрать из списка, как при успехе.
      if (error instanceof ApiError && error.status === 404) void onDeleted(comment.id);
    },
  });

  // Фокус: при подтверждении — на «Удалить» в подтверждении, после «Отмены» — обратно.
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
    else if (wasConfirming.current) deleteRef.current?.focus();
    wasConfirming.current = confirming;
  }, [confirming]);

  const channelPath = `/channel/${encodeURIComponent(comment.author.handle)}`;
  // Поля staff приходят только staff (иначе null) — по ним и показываем модерацию.
  const canBan = Boolean(comment.author_user_id) && comment.author_banned === false && comment.author_user_id !== user?.id;
  // 404 убирает комментарий, 401 обрабатывает сессия (интерфейс анонима) — без сообщения здесь.
  const removeError =
    remove.isError && !(remove.error instanceof ApiError && [401, 404].includes(remove.error.status))
      ? errorMessage(remove.error)
      : null;

  return (
    <li className={styles.item}>
      <Link to={channelPath} className={styles.avatar} aria-hidden="true" tabIndex={-1}>
        {initial(comment.author.title)}
      </Link>
      <div className={styles.body}>
        <div className={styles.meta}>
          <Link to={channelPath} className={styles.author}>
            <UserText text={comment.author.title} lines={1} />
          </Link>
          <time dateTime={comment.created_at} className={styles.muted}>
            {formatRelative(comment.created_at)}
          </time>
          {comment.author_banned && <span className={styles.badge}>Автор заблокирован</span>}
        </div>
        <UserText
          text={comment.body}
          as="p"
          id={bodyId}
          className={styles.text}
          lines={long && !expanded ? 8 : undefined}
        />
        {long && (
          <button
            type="button"
            className={styles.linkButton}
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? "Свернуть" : "Читать полностью"}
          </button>
        )}
        <div className={styles.actions}>
          {comment.can_delete &&
            (confirming ? (
              <div role="group" aria-label="Удалить комментарий?" className={styles.actions}>
                <span aria-hidden="true">Удалить комментарий?</span>
                <button
                  ref={confirmRef}
                  type="button"
                  className="button button-secondary"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate()}
                >
                  Удалить
                </button>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => {
                    remove.reset();
                    setConfirming(false);
                  }}
                >
                  Отмена
                </button>
              </div>
            ) : (
              <button ref={deleteRef} type="button" className={styles.linkButton} onClick={() => setConfirming(true)}>
                Удалить
              </button>
            ))}
          {comment.author_banned === true && comment.author_user_id && (
            <ReasonAction
              label="Разблокировать автора"
              reasonLabel="Снять блокировку? Комментарии автора снова станут видны всем."
              submitLabel="Разблокировать"
              required={false}
              withReason={false}
              onSubmit={async () => {
                await unbanUser(comment.author_user_id as string);
                await refreshAfterModeration(queryClient);
                onModerated("Автор разблокирован");
              }}
            />
          )}
          {canBan && comment.author_user_id && (
            <BanAuthorForm
              userId={comment.author_user_id}
              authorTitle={comment.author.title}
              onBanned={() => onModerated("Автор заблокирован")}
            />
          )}
        </div>
        {removeError && (
          <p className={styles.error} role="alert">
            {removeError}
          </p>
        )}
      </div>
    </li>
  );
}
