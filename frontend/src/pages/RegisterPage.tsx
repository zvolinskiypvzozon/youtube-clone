import { useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError, errorMessage, type FieldErrors } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { safeReturnPath } from "../auth/redirect";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import styles from "./AuthForm.module.css";
import { PASSWORD_MAX, serverFieldErrors, validateRegister } from "./authValidation";
import { FormField } from "./FormField";

export function RegisterPage() {
  useDocumentTitle("Регистрация");
  const { user, register } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const returnTo = safeReturnPath((location.state as { from?: unknown } | null)?.from);

  const [form, setForm] = useState({ email: "", username: "", password: "", confirm: "" });
  const [fields, setFields] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={returnTo} replace />;

  const set = (name: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [name]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const errors = validateRegister(form);
    setFields(errors);
    if (Object.keys(errors).length) return;
    setBusy(true);
    try {
      await register({ email: form.email.trim(), username: form.username.trim(), password: form.password });
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
        <h1 className={styles.title}>Регистрация</h1>
        <p className={styles.subtitle}>Вместе с аккаунтом создаётся ваш канал.</p>
        <form className={styles.form} onSubmit={submit} noValidate>
          {error && (
            <p className={styles.error} role="alert">
              {error}
            </p>
          )}
          <FormField id="email" label="Email" type="email" autoComplete="email" maxLength={254}
            value={form.email} error={fields.email} onChange={set("email")} />
          <FormField id="username" label="Имя пользователя" autoComplete="username" maxLength={30}
            hint="3–30 символов: латинские буквы, цифры и _. Станет адресом канала."
            value={form.username} error={fields.username} onChange={set("username")} />
          <FormField id="password" label="Пароль" type="password" autoComplete="new-password"
            maxLength={PASSWORD_MAX} hint="Не короче 8 символов."
            value={form.password} error={fields.password} onChange={set("password")} />
          <FormField id="confirm" label="Повторите пароль" type="password" autoComplete="new-password"
            maxLength={PASSWORD_MAX} value={form.confirm} error={fields.confirm} onChange={set("confirm")} />
          <div className={styles.actions}>
            <Link to="/login" state={location.state}>
              Уже есть аккаунт? Войти
            </Link>
            <button type="submit" className="button button-primary" disabled={busy}>
              {busy ? "Создаём…" : "Создать аккаунт"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
