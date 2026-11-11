import type { InputHTMLAttributes } from "react";
import styles from "./AuthForm.module.css";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  id: string;
  label: string;
  error?: string;
  hint?: string;
}

export function FormField({ id, label, error, hint, ...input }: Props) {
  const describedBy = [error ? `${id}-error` : "", hint ? `${id}-hint` : ""].filter(Boolean).join(" ");
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={styles.input}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy || undefined}
        {...input}
      />
      {hint && !error && (
        <span id={`${id}-hint`} className={styles.hint}>
          {hint}
        </span>
      )}
      {error && (
        <span id={`${id}-error`} className={styles.fieldError}>
          {error}
        </span>
      )}
    </div>
  );
}
