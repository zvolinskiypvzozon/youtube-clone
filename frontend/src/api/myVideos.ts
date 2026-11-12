import { request } from "./client";
import type { AccessEntry, AccessList, MyVideoPage, VideoCard, VideoUpdateIn } from "./types";

export const MY_VIDEOS_PAGE_SIZE = 24;
export const myVideosKey = ["my-videos"] as const;
export const accessKey = (videoId: string) => ["video-access", videoId] as const;

const videoPath = (id: string) => `/api/videos/${encodeURIComponent(id)}`;

export const fetchMyVideos = (cursor: string | null, signal?: AbortSignal) =>
  request<MyVideoPage>("/api/me/videos", { query: { cursor, limit: MY_VIDEOS_PAGE_SIZE }, signal });

/** Правка владельцем: только изменённые поля. */
export const updateVideo = (id: string, data: VideoUpdateIn) =>
  request<VideoCard>(videoPath(id), { method: "PATCH", body: data });

export const deleteVideo = (id: string) => request<void>(videoPath(id), { method: "DELETE" });

export const fetchAccess = (id: string, signal?: AbortSignal) =>
  request<AccessList>(`${videoPath(id)}/access`, { signal });

export const grantAccess = (id: string, username: string) =>
  request<AccessEntry>(`${videoPath(id)}/access`, { method: "POST", body: { username } });

export const revokeAccess = (id: string, userId: string) =>
  request<void>(`${videoPath(id)}/access/${encodeURIComponent(userId)}`, { method: "DELETE" });
