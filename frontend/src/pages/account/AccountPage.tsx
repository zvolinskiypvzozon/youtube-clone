import { useId, useState, type FormEvent } from "react";
import { ApiError, errorMessage } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { UserText } from "../../components/UserText";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import styles from "./AccountPage.module.css";

/** Аккаунт: данные и удаление с подтверждением паролем (день 9). */
export function AccountPage() {
  useDocumentTitle("Аккаунт");
  const { user, deleteAccount } = useAuth();
  const passwordId = useId();
  const errorId = useId();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!user) return null; // маршрут под RequireAuth

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!password) {
      setError("Введите пароль");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await deleteAccount(password);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 403 ? "Неверный пароль" : errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <section className={styles.page} aria-labelledby="account-title">
      <h1 id="account-title" className={styles.title}>
        Аккаунт
      </h1>
      <dl className={styles.facts}>
        <dt>Имя пользователя</dt>
        <dd>
          <UserText text={user.username} />
        </dd>
        <dt>Email</dt>
        <dd>
          <UserText text={user.email} />
        </dd>
      </dl>

      <form className={styles.danger} onSubmit={submit} noValidate aria-labelledby="delete-title">
        <h2 id="delete-title" className={styles.subtitle}>
          Удалить аккаунт
        </h2>
        <p className={styles.text}>
          Будут удалены канал, все ваши видео, плейлисты, комментарии, лайки и подписки. Это нельзя
          отменить. Имя пользователя «<UserText text={user.username} />» больше нельзя будет занять
          повторно.
        </p>
        <label htmlFor={passwordId} className={styles.label}>
          Пароль для подтверждения
        </label>
        <input
          id={passwordId}
          type="password"
          autoComplete="current-password"
          className={styles.input}
          value={password}
          maxLength={128}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(e) => {
            setPassword(e.target.value);
            setError(null);
          }}
        />
        {error && (
          <p id={errorId} className={styles.error} role="alert">
            {error}
          </p>
        )}
        <button type="submit" className={`button ${styles.dangerButton}`} disabled={busy}>
          Удалить аккаунт навсегда
        </button>
      </form>
    </section>
  );
}
