import { ApiError } from "./client";
import { api } from "./typed";
import type { LoginIn, Me, RegisterIn } from "./types";

/** Текущий пользователь или null (аноним; 401 — не ошибка). */
export async function fetchMe(signal?: AbortSignal): Promise<Me | null> {
  try {
    return await api("get", "/api/auth/me", { signal });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export const login = (data: LoginIn) => api("post", "/api/auth/login", { body: data });
export const register = (data: RegisterIn) => api("post", "/api/auth/register", { body: data });
export const logout = () => api("post", "/api/auth/logout");

/** Удаление своего аккаунта с подтверждением паролем (сервер очищает cookie сессии). */
export const deleteAccount = (password: string) => api("delete", "/api/me", { body: { password } });

