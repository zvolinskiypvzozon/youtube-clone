import { api } from "./typed";

export const channelKey = (handle: string) => ["channel", handle] as const;

/** Канал по нику. 404 — нет или автор заблокирован. */
export const fetchChannel = (handle: string, signal?: AbortSignal) =>
  api("get", "/api/channels/{handle}", { params: { handle }, signal });

/** Подписка: true — подписаться, false — отписаться (оба идемпотентны). */
export const setSubscription = (handle: string, subscribe: boolean) =>
  subscribe
    ? api("put", "/api/channels/{handle}/subscription", { params: { handle } })
    : api("delete", "/api/channels/{handle}/subscription", { params: { handle } });
