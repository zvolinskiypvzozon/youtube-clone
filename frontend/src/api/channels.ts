import { request } from "./client";
import type { Channel, SubscriptionOut } from "./types";

const channelPath = (handle: string) => `/api/channels/${encodeURIComponent(handle)}`;

export const channelKey = (handle: string) => ["channel", handle] as const;

/** Канал по нику. 404 — нет или автор заблокирован. */
export const fetchChannel = (handle: string, signal?: AbortSignal) =>
  request<Channel>(channelPath(handle), { signal });

/** Подписка: true — подписаться, false — отписаться (оба идемпотентны). */
export const setSubscription = (handle: string, subscribe: boolean) =>
  request<SubscriptionOut>(`${channelPath(handle)}/subscription`, { method: subscribe ? "PUT" : "DELETE" });
