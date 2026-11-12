import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, errorMessage } from "../../api/client";
import { BAN_REASON_MAX, banUser } from "../../api/comments";
import { refreshAfterModeration } from "../../api/moderation";
import { UserText } from "../../components/UserText";
import styles from "./Comments.module.css";

/**
 * Бан автора комментария (только staff: кнопка видна, когда сервер прислал author_user_id).
 * Кого можно банить (модератор — только пользователей, бан админа и т. п.), решает сервер:
 * его 403/409 показываются текстом.
 */
export function BanAuthorForm({
  userId,
  authorTitle,
  onBanned,
}: {
  userId: string;
  authorTitle: string;
  onBanned: () => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const fieldId = useId();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const mutation = useMutation({
    mutationFn: () => banUser(userId, reason),
    // Комментарии забаненного остаются видны staff с пометкой — перезапросить список. Сервер
    // пересчитал счётчики (комментарии, реакции, подписчики) всех затронутых видео и каналов.
    onSuccess: async () => {
      onBanned();
      await refreshAfterModeration(queryClient);
    },
  });

  // Фокус: при открытии — в поле причины, после «Отмены» — обратно на кнопку.
  const buttonRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) fieldRef.current?.focus();
    else if (wasOpen.current) buttonRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  if (!open) {
    return (
      <button ref={buttonRef} type="button" className={styles.linkButton} onClick={() => setOpen(true)}>
        Заблокировать автора
      </button>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (reason.trim()) mutation.mutate();
  }

  return (
    <form className={styles.banForm} onSubmit={submit} aria-label="Блокировка автора">
      <label htmlFor={fieldId} className={styles.fieldLabel}>
        Причина блокировки <UserText text={authorTitle} />
      </label>
      <textarea
        id={fieldId}
        ref={fieldRef}
        dir="auto"
        className={styles.textarea}
        rows={2}
        maxLength={BAN_REASON_MAX}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      {mutation.isError && !(mutation.error instanceof ApiError && mutation.error.status === 401) && (
        <p className={styles.error} role="alert">
          {errorMessage(mutation.error)}
        </p>
      )}
      <div className={styles.formBar}>
        <button
          type="button"
          className="button button-secondary"
          onClick={() => {
            mutation.reset();
            setOpen(false);
          }}
        >
          Отмена
        </button>
        <button type="submit" className="button button-primary" disabled={!reason.trim() || mutation.isPending}>
          Заблокировать
        </button>
      </div>
    </form>
  );
}
