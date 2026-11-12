import { useId, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { errorMessage } from "../../api/client";
import { accessKey, fetchAccess, grantAccess, revokeAccess } from "../../api/myVideos";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { formatRelative } from "../../lib/format";
import styles from "./MyVideosPage.module.css";

/** Как AccessGrantIn в API: ник 3–30 символов, латиница, цифры, «_». */
export const USERNAME_RE = /^[A-Za-z0-9_]{3,30}$/;

/** Допуск к своему приватному видео по нику (ADR 0006, дополнение 2). Права — на сервере. */
export function AccessPanel({ videoId, isPrivate }: { videoId: string; isPrivate: boolean }) {
  const queryClient = useQueryClient();
  const fieldId = useId();
  const [username, setUsername] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const access = useQuery({ queryKey: accessKey(videoId), queryFn: ({ signal }) => fetchAccess(videoId, signal) });
  const refresh = () => queryClient.invalidateQueries({ queryKey: accessKey(videoId) });
  const grant = useMutation({
    mutationFn: (name: string) => grantAccess(videoId, name),
    onSuccess: async () => {
      setUsername("");
      await refresh();
    },
  });
  const revoke = useMutation({ mutationFn: (userId: string) => revokeAccess(videoId, userId), onSuccess: refresh });

  function submit(event: FormEvent) {
    event.preventDefault();
    const name = username.trim();
    if (!USERNAME_RE.test(name)) {
      setFieldError("Ник: 3–30 символов, латинские буквы, цифры и «_»");
      return;
    }
    setFieldError(null);
    grant.mutate(name);
  }

  const items = access.data?.items ?? [];
  const full = access.data ? items.length >= access.data.max_entries : false;

  return (
    <div className={styles.panel} role="group" aria-label="Доступ к видео">
      <p className={styles.panelText}>
        {isPrivate
          ? "Видео с ограниченным доступом видят только вы и пользователи из списка."
          : "Список действует, только когда у видео ограниченный доступ: сейчас видео видят все, у кого есть ссылка."}
      </p>
      <form className={styles.inline} onSubmit={submit} noValidate>
        <label htmlFor={fieldId} className="visually-hidden">
          Ник пользователя
        </label>
        <input
          id={fieldId}
          className={styles.input}
          placeholder="Ник пользователя"
          value={username}
          maxLength={30}
          aria-invalid={fieldError ? true : undefined}
          onChange={(e) => {
            setUsername(e.target.value);
            setFieldError(null);
            grant.reset();
          }}
        />
        <button type="submit" className="button button-secondary" disabled={grant.isPending || full}>
          Открыть доступ
        </button>
      </form>
      {(fieldError || grant.isError) && (
        <p className={styles.error} role="alert">
          {fieldError ?? errorMessage(grant.error)}
        </p>
      )}
      {full && <p className={styles.panelText}>Достигнут лимит списка: {access.data?.max_entries}.</p>}
      {access.isPending ? (
        <Spinner />
      ) : access.isError ? (
        <StateMessage title="Не удалось загрузить список доступа" role="alert" onRetry={() => void access.refetch()}>
          {errorMessage(access.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <p className={styles.panelText}>Доступ пока никому не открыт.</p>
      ) : (
        <ul className={styles.accessList}>
          {items.map((entry) => (
            <li key={entry.user_id} className={styles.accessItem}>
              <UserText text={entry.username} lines={1} />
              <time dateTime={entry.granted_at} className={styles.panelText}>
                {formatRelative(entry.granted_at)}
              </time>
              <button
                type="button"
                className={styles.linkButton}
                disabled={revoke.isPending}
                aria-label={`Закрыть доступ: ${entry.username}`}
                onClick={() => revoke.mutate(entry.user_id)}
              >
                Закрыть доступ
              </button>
            </li>
          ))}
        </ul>
      )}
      {revoke.isError && (
        <p className={styles.error} role="alert">
          {errorMessage(revoke.error)}
        </p>
      )}
    </div>
  );
}
