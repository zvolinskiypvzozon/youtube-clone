import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { errorMessage } from "../../api/client";
import type { ReportCategory } from "../../api/types";
import { createReport } from "../../api/videos";
import { useAuth } from "../../auth/AuthContext";
import { useLoginRedirect } from "../../auth/useLoginRedirect";
import styles from "./WatchPage.module.css";

export const REPORT_COMMENT_MAX = 500;

const CATEGORIES: [ReportCategory, string][] = [
  ["spam", "Спам или обман"],
  ["violence", "Насилие или жестокость"],
  ["adult", "Контент для взрослых"],
  ["copyright", "Нарушение авторских прав"],
  ["other", "Другое"],
];

/** Жалоба на видео: категория и необязательное пояснение. Владельцу видео не показывается. */
export function ReportForm({ videoId }: { videoId: string }) {
  const { user } = useAuth();
  const toLogin = useLoginRedirect();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<ReportCategory | null>(null);
  const [comment, setComment] = useState("");
  const commentId = useId();
  const counterId = useId();
  // Фокус: при открытии — на форму (кнопка «Пожаловаться» исчезает), после «Отмены» — обратно.
  const formRef = useRef<HTMLFormElement>(null);
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) formRef.current?.querySelector<HTMLInputElement>("input")?.focus();
    else if (wasOpen.current) openButtonRef.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const mutation = useMutation({
    mutationFn: () =>
      createReport(videoId, { category: category as ReportCategory, comment: comment.trim() || null }),
  });

  if (mutation.isSuccess) {
    return (
      <p className={styles.notice} role="status">
        Спасибо, жалоба отправлена. Её рассмотрит модератор.
      </p>
    );
  }

  if (!open) {
    return (
      <button
        ref={openButtonRef}
        type="button"
        className="button button-secondary"
        disabled={user === undefined}
        onClick={() => (user ? setOpen(true) : toLogin())}
      >
        Пожаловаться
      </button>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (category) mutation.mutate();
  }

  return (
    <form ref={formRef} className={styles.report} onSubmit={submit} aria-label="Жалоба на видео">
      <fieldset className={styles.reportFieldset}>
        <legend>Что не так с видео?</legend>
        {CATEGORIES.map(([value, label]) => (
          <label key={value} className={styles.radio}>
            <input
              type="radio"
              name="category"
              value={value}
              checked={category === value}
              onChange={() => setCategory(value)}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <label htmlFor={commentId} className={styles.fieldLabel}>
        Пояснение (необязательно)
      </label>
      <textarea
        id={commentId}
        dir="auto"
        className={styles.textarea}
        maxLength={REPORT_COMMENT_MAX}
        rows={3}
        aria-describedby={counterId}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <span id={counterId} className={styles.counter}>
        {comment.length} / {REPORT_COMMENT_MAX}
      </span>
      {mutation.isError && (
        <p className={styles.inlineError} role="alert">
          {errorMessage(mutation.error)}
        </p>
      )}
      <div className={styles.formActions}>
        <button type="button" className="button button-secondary" onClick={() => setOpen(false)}>
          Отмена
        </button>
        <button type="submit" className="button button-primary" disabled={!category || mutation.isPending}>
          Отправить
        </button>
      </div>
    </form>
  );
}
