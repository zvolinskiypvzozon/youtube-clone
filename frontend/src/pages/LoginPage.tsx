import { useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, errorMessage, type FieldErrors } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { safeReturnPath } from "../auth/redirect";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import styles from "./AuthForm.module.css";
import { serverFieldErrors, validateLogin } from "./authValidation";
import { FormField } from "./FormField";

export function LoginPage() {
  useDocumentTitle("Вход");
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const returnTo = safeReturnPath((location.state as { from?: unknown } | null)?.from);

  const [form, setForm] = useState({ login: "", password: "" });
  const [fields, setFields] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={returnTo} replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const errors = validateLogin(form);
    setFields(errors);
    if (Object.keys(errors).length) return;
    setBusy(true);
    try {
      await login({ login: form.login.trim(), password: form.password });
      navigate(returnTo, { replace: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) setFields(serverFieldErrors(e.fields));
      else setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <h1 className={styles.title}>Вход</h1>
        <p className={styles.subtitle}>Войдите, чтобы ставить лайки, комментировать и загружать видео.</p>
        <form className={styles.form} onSubmit={submit} noValidate>
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
          <FormField
            id="login"
            label="Email или имя пользователя"
            autoComplete="username"
            maxLength={254}
            value={form.login}
            error={fields.login}
            onChange={(e) => setForm({ ...form, login: e.target.value })}
          />
          <FormField
            id="password"
            label="Пароль"
            type="password"
            autoComplete="current-password"
            maxLength={128}
            value={form.password}
            error={fields.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <div className={styles.actions}>
            <Link to="/register" state={location.state}>
              Создать аккаунт
            </Link>
            <button type="submit" className="button button-primary" disabled={busy}>
              {busy ? "Входим…" : "Войти"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
