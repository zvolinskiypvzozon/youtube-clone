import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, errorMessage } from "../api/client";
import { MODERATION_REASON_MAX } from "../api/moderation";
import { onUnauthorized } from "../api/queryClient";
import styles from "./ReasonAction.module.css";

interface Props {
  /** Текст кнопки, открывающей действие. */
  label: string;
  /** Подпись поля причины (или текст подтверждения, если причины нет). */
  reasonLabel: string;
  submitLabel: string;
  /** Причина обязательна (скрытие, бан) или нет (отклонение жалобы). */
  required: boolean;
  /** Без причины — только подтверждение (возврат видео, разбан). */
  withReason?: boolean;
  onSubmit: (reason: string) => Promise<unknown>;
}

/**
 * Действие модерации: кнопка → форма с причиной (или подтверждение) → запрос. Ошибки сервера
 * (403 «своё видео», бан админа, 409) — текстом у формы. Фокус: при открытии — в поле или на
 * подтверждение, после «Отмены» — обратно на кнопку; после успеха кнопка обычно исчезает —
 * фокус и объявление переносит вызывающий (ревью дня 8). 401 — интерфейс анонима, как у
 * остальных запросов (аудит дня 8): запрос идёт не через useMutation.
 */
export function ReasonAction({ label, reasonLabel, submitLabel, required, withReason = true, onSubmit }: Props) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fieldId = useId();
  const errorId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const firstRef = useRef<HTMLTextAreaElement & HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  useEffect(() => {
    if (open) firstRef.current?.focus();
    else if (wasOpen.current) buttonRef.current?.focus();
    wasOpen.current = open;
  }, [open]);

  function close() {
    setOpen(false);
    setReason("");
    setError(null);
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (withReason && required && !reason.trim()) {
      setError("Укажите причину");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(reason.trim());
      setOpen(false);
      setReason("");
    } catch (e) {
      onUnauthorized(queryClient, e);
      setError(e instanceof ApiError && e.status === 422 ? (e.fields.reason ?? e.message) : errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button ref={buttonRef} type="button" className="button button-secondary" onClick={() => setOpen(true)}>
        {label}
      </button>
    );
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-label={label} aria-busy={busy || undefined}>
      {withReason ? (
        <>
          <label htmlFor={fieldId} className={styles.label}>
            {reasonLabel}
            {!required && " (необязательно)"}
          </label>
          <textarea
            id={fieldId}
            ref={firstRef}
            dir="auto"
            className={styles.textarea}
            rows={2}
            maxLength={MODERATION_REASON_MAX}
            value={reason}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : undefined}
            onChange={(e) => setReason(e.target.value)}
          />
        </>
      ) : (
        <p className={styles.muted}>{reasonLabel}</p>
      )}
      {error && (
        <p id={errorId} className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button ref={withReason ? undefined : firstRef} type="submit" className="button button-primary" disabled={busy}>
          {submitLabel}
        </button>
        {/* Во время запроса «Отмена» недоступна: закрыть форму, а действие всё равно выполнится
            — обман (ревью дня 8). */}
        <button type="button" className="button button-secondary" onClick={close} disabled={busy}>
          Отмена
        </button>
      </div>
    </form>
  );
}
