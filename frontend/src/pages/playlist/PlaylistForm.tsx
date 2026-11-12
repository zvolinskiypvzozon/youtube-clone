import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { ApiError, errorMessage, type FieldErrors } from "../../api/client";
import { PLAYLIST_DESCRIPTION_MAX, PLAYLIST_TITLE_MAX } from "../../api/playlists";
import type { PlaylistVisibility } from "../../api/types";
import styles from "./PlaylistForm.module.css";

export const PLAYLIST_VISIBILITY_OPTIONS: [PlaylistVisibility, string, string][] = [
  ["public", "Открытый", "виден на странице канала"],
  ["unlisted", "По ссылке", "открывается только по ссылке"],
  ["private", "Только я", "видите только вы"],
];

export interface PlaylistValues {
  title: string;
  description: string;
  visibility: PlaylistVisibility;
}

interface Props {
  initial?: PlaylistValues;
  submitLabel: string;
  /** Без описания (короткая форма в меню «Сохранить»). */
  compact?: boolean;
  onSubmit: (values: PlaylistValues) => Promise<unknown>;
  onCancel: () => void;
}

/** Создание и правка плейлиста. Ошибки 422 — у полей, прочие — общим текстом. */
export function PlaylistForm({ initial, submitLabel, compact, onSubmit, onCancel }: Props) {
  const ids = { title: useId(), description: useId() };
  const [values, setValues] = useState<PlaylistValues>(initial ?? { title: "", description: "", visibility: "public" });
  const [fields, setFields] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const titleRef = useRef<HTMLInputElement>(null);
  useEffect(() => titleRef.current?.focus(), []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!values.title.trim()) {
      setFields({ title: "Введите название" });
      return;
    }
    setFields({});
    setBusy(true);
    try {
      await onSubmit(values);
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && (e.fields.title || e.fields.description)) setFields(e.fields);
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-label={submitLabel}>
      <label htmlFor={ids.title} className={styles.label}>
        Название плейлиста
      </label>
      <input
        id={ids.title}
        ref={titleRef}
        dir="auto"
        className={styles.input}
        maxLength={PLAYLIST_TITLE_MAX}
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
      {!compact && (
        <>
          <label htmlFor={ids.description} className={styles.label}>
            Описание
          </label>
          <textarea
            id={ids.description}
            dir="auto"
            className={styles.input}
            rows={3}
            maxLength={PLAYLIST_DESCRIPTION_MAX}
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
        </>
      )}
      <fieldset className={styles.radios}>
        <legend className={styles.label}>Доступ</legend>
        {PLAYLIST_VISIBILITY_OPTIONS.map(([value, label, hint]) => (
          <label key={value} className={styles.radio}>
            <input
              type="radio"
              name={`playlist-visibility-${ids.title}`}
              value={value}
              checked={values.visibility === value}
              onChange={() => setValues({ ...values, visibility: value })}
            />
            <span>
              {label} <span className={styles.hint}>— {hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.actions}>
        <button type="submit" className="button button-primary" disabled={busy}>
          {submitLabel}
        </button>
        <button type="button" className="button button-secondary" onClick={onCancel}>
          Отмена
        </button>
      </div>
    </form>
  );
}
