import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "../api/client";
import type { Me } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { UserText } from "./UserText";
import styles from "./UserMenu.module.css";

/** Меню пользователя — раскрывающийся блок (disclosure) со ссылками и кнопкой выхода. */
export function UserMenu({ user }: { user: Me }) {
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    const onFocus = (e: FocusEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    document.addEventListener("focusin", onFocus);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("focusin", onFocus);
    };
  }, [open]);

  async function onLogout() {
    setError(null);
    setBusy(true);
    try {
      await logout(); // при успехе компонент размонтируется (шапка покажет «Войти»)
    } catch (e) {
      // Сессия жива (CSRF, сеть, 5xx): остаёмся вошедшими и показываем причину.
      setError(`Не удалось выйти. ${errorMessage(e)}`);
      setOpen(false);
      setBusy(false);
    }
  }

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className={styles.avatar}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`Меню пользователя ${user.username}`}
        onClick={() => setOpen((v) => !v)}
      >
        {user.username.slice(0, 1).toUpperCase()}
      </button>
      {open && (
        <div id={panelId} className={styles.menu}>
          <div className={styles.head}>
            <UserText text={user.channel.title} className={styles.name} lines={1} />
            <span className={styles.handle}>@{user.username}</span>
          </div>
          <Link className={styles.item} to={`/channel/${encodeURIComponent(user.channel.handle)}`} onClick={() => setOpen(false)}>
            Мой канал
          </Link>
          <Link className={styles.item} to="/account" onClick={() => setOpen(false)}>
            Аккаунт
          </Link>
          <Link className={styles.item} to="/my/videos" onClick={() => setOpen(false)}>
            Мои видео
          </Link>
          <button type="button" className={styles.item} onClick={onLogout} disabled={busy}>
            Выйти
          </button>
        </div>
      )}
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
