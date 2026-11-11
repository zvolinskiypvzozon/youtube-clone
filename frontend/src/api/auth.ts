import { ApiError, request } from "./client";
import type { LoginIn, Me, RegisterIn } from "./types";

/** Текущий пользователь или null (аноним; 401 — не ошибка). */
export async function fetchMe(signal?: AbortSignal): Promise<Me | null> {
  try {
    return await request<Me>("/api/auth/me", { signal });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export const login = (data: LoginIn) => request<Me>("/api/auth/login", { method: "POST", body: data });
export const register = (data: RegisterIn) =>
  request<Me>("/api/auth/register", { method: "POST", body: data });
export const logout = () => request<void>("/api/auth/logout", { method: "POST" });
