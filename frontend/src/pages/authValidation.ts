// Проверки форм входа и регистрации — те же правила, что в схемах API (app/schemas/auth.py).
// Сообщения сервера в 422 на английском (Pydantic), поэтому для полей показываем свои.
import type { FieldErrors } from "../api/client";

export const USERNAME_RE = /^[A-Za-z0-9_]{3,30}$/;
export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 128;

export const FIELD_MESSAGES: Record<string, string> = {
  email: "Введите корректный email.",
  username: "Имя: 3–30 символов — латинские буквы, цифры и _.",
  password: `Пароль: от ${PASSWORD_MIN} до ${PASSWORD_MAX} символов.`,
  login: "Введите email или имя пользователя (не короче 3 символов).",
};

/** Ошибки полей из ответа 422 → русские сообщения. */
export function serverFieldErrors(fields: FieldErrors): FieldErrors {
  return Object.fromEntries(
    Object.keys(fields).map((name) => [name, FIELD_MESSAGES[name] ?? "Неверное значение."]),
  );
}

export function validateRegister(v: {
  email: string;
  username: string;
  password: string;
  confirm: string;
}): FieldErrors {
  const e: FieldErrors = {};
  if (!/^[^\s@]+@[^\s@]+$/.test(v.email.trim()) || v.email.length > 254) e.email = FIELD_MESSAGES.email;
  if (!USERNAME_RE.test(v.username.trim())) e.username = FIELD_MESSAGES.username;
  if (v.password.length < PASSWORD_MIN || v.password.length > PASSWORD_MAX) e.password = FIELD_MESSAGES.password;
  if (v.confirm !== v.password) e.confirm = "Пароли не совпадают.";
  return e;
}

export function validateLogin(v: { login: string; password: string }): FieldErrors {
  const e: FieldErrors = {};
  if (v.login.trim().length < 3) e.login = FIELD_MESSAGES.login;
  if (!v.password) e.password = "Введите пароль.";
  return e;
}
